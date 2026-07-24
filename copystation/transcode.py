"""Optional video transcoding / resolution change via ffmpeg.

Jobs are submitted from the web interface. A single background worker runs them
one at a time; while a job runs it holds the shared ``operation_lock`` (so the
copy daemon never mounts/writes the same removable volume at the same time),
mounts the OUTPUT volume read-write, and writes the result into a
``<output_dirname>/`` folder on it. The output is then downloadable through the
normal file browser.

``build_ffmpeg_cmd`` and the filename/duration helpers are pure and unit-tested;
the actual ffmpeg/ffprobe execution is field-validated on the device. The whole
feature is a no-op with a clear error when ffmpeg is not installed.
"""

from __future__ import annotations

import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional

from .encoders import (
    Encoder,
    available_encoders,
    available_gst_elements,
    available_hwaccels,
    build_ffmpeg_cmd,
    build_gstreamer_cmd,
    build_hevc_crop_remux_cmd,
    cpu_encoder,
    decoder_scale_factor,
    default_bitrate,
    detect_board,
    gst_can_handle,
    gstreamer_output_height,
    hevc_conformance_crop,
    select_encoders,
    with_decode_offload,
)
from .mounts import BrowseError, NotFound, safe_resolve
from .settings_store import DEFAULT_USER_SETTINGS_FILE, SettingsStore
from .status import State

_LOG = logging.getLogger("copystation.transcode")

# Keep at most this many finished/failed jobs in the visible history.
MAX_HISTORY = 20

# Kill a GStreamer encode that produces no output at all for this long: the
# Allwinner OMX stack can wedge on a bad decoder->encoder handoff, and a stuck
# hardware job must not hang the (headless) station. A healthy pipeline emits a
# ``progressreport`` line every 2 s, so a long silence means it is stuck; the
# kill surfaces as a normal failure and falls back to the CPU encoder.
GST_STALL_SECONDS = 90

# A canceled single-pass job still trains the estimate model once it has produced
# at least this many seconds of output -- enough that the startup transient
# (mount, preroll, first GOP) is amortized and the measured seconds-per-frame is
# stable. Two-stage jobs are not tracked this way (a partial measurement is not
# representative of the total), so they only train the model on full completion.
PERF_STABLE_MIN_OUTPUT_SEC = 10

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Where a transcoded file is written -- always on the SOURCE file's own medium
# (the output device is never chosen separately), only the sub-directory differs:
#   "central" -> <volume-root>/<output_dirname>/       (one folder per medium)
#   "same"    -> <source-dir>/<output_dirname>/         (a Transcoded folder beside
#                                                        the source file)
OUTPUT_LOCATIONS = ("central", "same")
DEFAULT_OUTPUT_LOCATION = "same"

# Extensions treated as transcodable video when a whole folder is submitted. The
# GStreamer hardware path only takes a subset (see ``encoders._GST_DEMUX``); the
# rest still transcode on the CPU. Non-video files (photos, sidecars) are skipped.
VIDEO_EXTS = frozenset({
    "mp4", "mov", "m4v", "mkv", "webm", "avi", "mts", "m2ts", "ts",
    "mpg", "mpeg", "wmv", "flv", "3gp", "3g2", "mxf", "insv",
})
# Deliberately NOT here: ``.lrv`` (DJI/Insta360 low-resolution preview proxies) --
# they are not worth transcoding and share a stem with the real clip, so they
# would only collide on the output name (e.g. DJI_0001.LRV vs DJI_0001.MP4).


def is_video_file(name: str) -> bool:
    """Whether ``name`` has a known video extension (for folder submission)."""
    return Path(str(name)).suffix.lower().lstrip(".") in VIDEO_EXTS


class TranscodeError(Exception):
    """Base class for transcode errors (mapped to HTTP codes by the web layer)."""


class TranscodeUnavailable(TranscodeError):
    """ffmpeg is not installed / the feature is off."""


class UnknownPreset(TranscodeError):
    """The requested preset id is not configured."""


class InvalidSetting(TranscodeError):
    """A runtime setting was given an unsupported value (mapped to 400)."""


class TranscodeBusy(TranscodeError):
    """Refused because a copy or another transcode is in progress (mapped to 409)."""


class _Canceled(TranscodeError):
    """Internal: the running ffmpeg was canceled -- do not fall back or retry."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def mem_available_bytes(path: str = "/proc/meminfo") -> int:
    """Free RAM in bytes from ``MemAvailable`` in /proc/meminfo (0 if unknown)."""
    try:
        for line in Path(path).read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024  # kB -> bytes
    except (OSError, ValueError, IndexError):
        pass
    return 0


def ram_budget(free_bytes: int, fraction: float) -> int:
    """The RAM the buffer may use: ``fraction`` of the free RAM (>= 0)."""
    return max(0, int(free_bytes * float(fraction)))


def parse_bitrate(value: Any) -> int:
    """Bits/second from an ffmpeg-style bitrate ('8M', '2500k', 8000000)."""
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value or "").strip().lower()
    if not s:
        return 0
    mult = 1
    if s.endswith("m"):
        mult, s = 1_000_000, s[:-1]
    elif s.endswith("k"):
        mult, s = 1_000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def estimate_output_bytes(duration: Optional[float], preset: dict,
                          audio_bps: int = 128_000, safety: float = 1.5) -> int:
    """Rough transcoded-file size (bytes) for sizing the RAM output buffer.

    Only the *output* is buffered in RAM (the input streams from the card), so
    this -- not the input size -- decides whether the buffer is used. Uses the
    preset's ``bitrate`` (or a height-based default) plus audio, with headroom.
    Returns 0 when the duration is unknown (then we stream to the card).
    """
    if not duration or duration <= 0:
        return 0
    height = int(preset.get("height", 0) or 0)
    vbps = parse_bitrate(preset.get("bitrate") or default_bitrate(height))
    return int((vbps + audio_bps) / 8 * duration * safety)


def sanitize_component(name: str) -> str:
    """Reduce a filename to a safe single path component."""
    base = Path(str(name)).name  # strip any directory part
    cleaned = _SAFE_CHARS.sub("_", base).strip("._")
    return cleaned or "output"


def output_name(input_name: str, preset_id: str, container: str = "mp4") -> str:
    """Output filename: ``<stem>_<preset>.<container>`` (sanitised)."""
    stem = sanitize_component(Path(str(input_name)).stem)
    preset = sanitize_component(preset_id)
    return f"{stem}_{preset}.{container}"


def unique_output_path(path: Path) -> Path:
    """``path``, or the first free ``<stem>_<n><suffix>`` if it already exists.

    Guards against silently overwriting an existing output -- most importantly two
    different sources in a batch that map to the same name (``DJI_0001.MP4`` and
    ``DJI_0001.MOV`` both produce ``DJI_0001_<preset>.mp4``), but also a re-run of
    the same file. The single transcode worker runs under the operation lock, so
    the existence check cannot race another job.
    """
    if not path.exists():
        return path
    parent, stem, suffix = path.parent, path.stem, path.suffix
    n = 2
    while True:
        candidate = parent / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def probe_duration(src: Any) -> Optional[float]:
    """Media duration in seconds via ffprobe, or ``None`` if unknown."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(src)],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def probe_video_info(src: Any) -> dict:
    """Video codec, dimensions, container and audio of ``src`` via ffprobe.

    Feeds ``encoders.gst_can_handle`` / ``build_gstreamer_cmd`` (the hardware
    GStreamer path needs the source codec to pick the HW decoder, the dimensions
    to compute the downscaled width, and the audio codec to decide whether it can
    stream-copy the audio). Best-effort: on any error the fields stay at their
    empty defaults, which makes ``gst_can_handle`` return False (CPU fallback).
    """
    info: dict = {
        "vcodec": None, "width": 0, "height": 0, "fps": 0.0, "duration": None,
        "has_audio": False, "acodec": None,
        "container": Path(str(src)).suffix.lower().lstrip("."),
    }
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate"
             ":stream_disposition=attached_pic:format=duration",
             "-of", "json", str(src)],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
        for st in data.get("streams", []):
            kind = st.get("codec_type")
            if kind == "video" and info["vcodec"] is None:
                # Skip embedded cover art / thumbnails (e.g. the DJI MJPEG preview).
                if (st.get("disposition") or {}).get("attached_pic"):
                    continue
                info["vcodec"] = st.get("codec_name")
                info["width"] = int(st.get("width") or 0)
                info["height"] = int(st.get("height") or 0)
                info["fps"] = _parse_fps(st.get("avg_frame_rate")) or \
                    _parse_fps(st.get("r_frame_rate"))
            elif kind == "audio" and not info["has_audio"]:
                info["has_audio"] = True
                info["acodec"] = st.get("codec_name")
        try:
            info["duration"] = float((data.get("format") or {}).get("duration"))
        except (TypeError, ValueError):
            pass
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError,
            AttributeError, TypeError):
        pass
    return info


def probe_coded_dims(src: Any) -> tuple:
    """``(coded_width, coded_height)`` of the first video stream via ffprobe, or ``(0, 0)``.

    The *coded* size includes the encoder's coding-block padding (e.g. 1088 for a
    1080-line HEVC frame). Used to compute the conformance-window crop that the
    Allwinner OMX HEVC encoder omits (see ``encoders.hevc_conformance_crop``).
    Best-effort: any failure returns ``(0, 0)``, which makes the crop a no-op.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=coded_width,coded_height", "-of", "json", str(src)],
            capture_output=True, text=True, check=True,
        ).stdout
        streams = json.loads(out).get("streams") or []
        st = streams[0] if streams else {}
        return int(st.get("coded_width") or 0), int(st.get("coded_height") or 0)
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError,
            IndexError, TypeError):
        return 0, 0


def _parse_fps(rate: Any) -> float:
    """Frames/second from an ffprobe ``num/den`` rate string (0.0 if unknown)."""
    try:
        num, den = str(rate).split("/")
        num, den = float(num), float(den)
        return num / den if den else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def _hms_to_seconds(value: str) -> Optional[float]:
    try:
        h, m, s = value.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def progress_seconds(line: str) -> Optional[float]:
    """Elapsed output seconds from one ffmpeg ``-progress`` line, else ``None``.

    Handles the several shapes ffmpeg builds emit: ``out_time=HH:MM:SS.us``,
    ``out_time_us=<micros>`` and ``out_time_ms=<micros>`` (ffmpeg's ``_ms`` field
    is actually microseconds).
    """
    if "=" not in line:
        return None
    key, _, raw = line.partition("=")
    raw = raw.strip()
    if key == "out_time":
        return _hms_to_seconds(raw)
    if key in ("out_time_us", "out_time_ms"):
        try:
            return int(raw) / 1_000_000
        except ValueError:
            return None
    return None


# A ``progressreport`` line looks like:
#   progressreport0 (00:00:12): 7 / 56 seconds (12.5 %)
_GST_PERCENT = re.compile(r"\(\s*([0-9]+(?:\.[0-9]+)?)\s*%\)")
_GST_POSITION = re.compile(r"(\d+)\s*/\s*\d+\s*seconds")


def gst_progress_percent(line: str) -> Optional[float]:
    """Percent complete from one GStreamer ``progressreport`` line, else ``None``."""
    m = _GST_PERCENT.search(line)
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            return None
    return None


def gst_progress_position(line: str) -> Optional[int]:
    """Output seconds processed so far from a ``progressreport`` line, else ``None``.

    GStreamer's ``progressreport`` reports no encode rate, so the transcoder
    derives speed/fps from this position over the elapsed wall time.
    """
    m = _GST_POSITION.search(line)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def perf_key(info: dict, preset_id: str) -> str:
    """Stable performance-model key: source codec + resolution + preset id.

    Keyed by the source parameters that dominate transcode time (codec and
    resolution) plus the preset (which fixes the target and the HW/CPU path).
    Framerate is not in the key -- it is folded into the estimate as a frame count.
    """
    return (f"{str(info.get('vcodec') or '?').lower()}:"
            f"{int(info.get('width') or 0)}x{int(info.get('height') or 0)}:{preset_id}")


def estimate_seconds(spf: Optional[float], duration: Optional[float],
                     fps: Optional[float]) -> Optional[float]:
    """Estimated wall time = seconds-per-frame x frame count (duration x fps).

    Framerate is folded in via the frame count, so a source at double the fps of
    the learned sample estimates to double the time. ``None`` when unknown.
    """
    if not spf or not duration or not fps or duration <= 0 or fps <= 0:
        return None
    return spf * duration * fps


# Seed performance model (wall-seconds per source frame) **per board**, so a fresh
# install can estimate a job's duration before it has run one. These are hardware-
# specific -- the Cubie A7S numbers do NOT transfer to the Raspberry Pi 4/5, which
# use different encoders -- so they are keyed by the detected board. Within a board
# the keys match the learned model (see ``perf_key``); a real job overwrites the
# seed for its key on its first run, and per-install learned values take precedence.
# Values were measured on-device 2026-07-11/12 with a 4K60 H.264 source and, for the
# ``hevc:`` keys, a 4K/1080p HEVC clip. ``spf`` = 1 / (frames-per-wall-second), so a
# fresh install estimates a job before it has run one; a real job then overwrites the
# seed for its key. Every board+source-codec+target we benchmarked is seeded here.
# ``generic`` stays unseeded -> no estimate until a job trains the model.
DEFAULT_PERF: dict = {
    # Cubie A7S (Allwinner A733): H.264 sources use the GStreamer OMX hardware
    # pipeline; BOTH H.264 and H.265 output are hardware-encoded (omxh264videoenc /
    # omxhevcvideoenc). A clean 1/2-step output (1080p / 540p from 4K) is a single
    # hardware pass ~0.66x with the CPU idle -- H.265 output is just as fast as H.264
    # (1080p-h265 = 0.0255, measured 2026-07-12). 720p adds a CPU finishing pass;
    # 720p-h265 finishes with CPU libx265 (the bottleneck). An HEVC source runs the
    # same pipeline but with the HEVC decoder, which on the A733 is FASTER than the
    # H.264 decoder -- so the hevc: rows are faster than the h264: ones at the same
    # target (measured 2026-07-12 with a 4K H.265 clip).
    "cubie": {
        "h264:3840x2160:1080p-h264": {"spf": 0.0251},  # single hardware pass
        "h264:3840x2160:1080p-h265": {"spf": 0.0255},  # single HW pass (HW HEVC encode)
        "h264:3840x2160:540p-h264": {"spf": 0.0243},   # single hardware pass
        "h264:3840x2160:720p-h264": {"spf": 0.0500},   # hardware 1080p + CPU finish
        "h264:3840x2160:720p-h265": {"spf": 0.1603},   # HW HEVC 1080p + CPU libx265 finish
        "hevc:3840x2160:1080p-h264": {"spf": 0.0170},  # single HW pass (59 fps)
        "hevc:3840x2160:540p-h264": {"spf": 0.0164},   # single HW pass (61 fps)
        "hevc:3840x2160:720p-h264": {"spf": 0.0396},   # HW 1080 + CPU finish (25 fps)
        "hevc:3840x2160:720p-h265": {"spf": 0.1512},   # HW HEVC 1080 + CPU libx265 finish (6.6 fps)
    },
    # Raspberry Pi 5: NO hardware encoder, so every output is a libx264/libx265 CPU
    # encode (preset veryfast). H.264 4K input decodes on the CPU too; HEVC input is
    # hardware-decoded (-hwaccel drm), which is why the hevc keys are faster than the
    # h264 ones at the same target. Measured on-device 2026-07-11 (the Pi 5 thermally
    # soft-throttled at ~80 C under sustained load, so real jobs land here).
    "pi5": {
        "h264:3840x2160:1080p-h264": {"spf": 0.0476},  # CPU decode + libx264 1080p
        "h264:3840x2160:720p-h264": {"spf": 0.0313},   # CPU decode + libx264 720p
        "h264:3840x2160:540p-h264": {"spf": 0.0270},   # CPU decode + libx264 540p
        "h264:3840x2160:720p-h265": {"spf": 0.0625},   # CPU decode + libx265 720p
        "hevc:3840x2160:1080p-h264": {"spf": 0.0357},  # HW decode + libx264 1080p (28 fps)
        "hevc:3840x2160:720p-h264": {"spf": 0.0208},   # HW decode + libx264 720p (48 fps)
        "hevc:3840x2160:540p-h264": {"spf": 0.0161},   # HW decode + libx264 540p (62 fps)
        "hevc:3840x2160:720p-h265": {"spf": 0.0526},   # HW decode + libx265 720p (19 fps)
        "hevc:1920x1080:1080p-h264": {"spf": 0.0263},  # 1080p HEVC source (38 fps)
    },
    # Raspberry Pi 4: has a hardware H.264 *encoder* (h264_v4l2m2m) AND the same
    # HEVC hardware *decoder* as the Pi 5 (-hwaccel drm). Measured on-device
    # 2026-07-12 (Pi 4B rev 1.5, kernel 6.12, ffmpeg rpt). Two caveats baked into
    # these: (1) a 4K H.264 source is CPU-DECODE-bound (~24 fps ceiling on the A72),
    # so the target resolution barely moves the H.264-source rows; (2) the HW H.264
    # encoder defaults to H.264 level 4.0 (~1080p30) and ffmpeg does not raise it, so
    # a 1080p output above 30 fps (this source is 60 fps) exceeds the level and the
    # driver rejects it (enforced since kernel 6.6.31) -- 1080p H.264 then runs on the
    # slow CPU. A <=30 fps 1080p source, and 720p at 60 fps, stay on the HW encoder.
    # HEVC input is HW-decoded, and HEVC->720p H.264 is a near-full-HW pass. Note the
    # HEVC-source rows are HEVC-DECODE-bound (4K HEVC HW-decodes at ~22 fps; the HW
    # H.264 encode adds almost nothing), so they scale with the source's decode
    # complexity -- the 540p seed was measured on a heavier 4K100 HEVC clip (17 fps),
    # which is why it lands below the lighter-clip 720p value.
    "pi4": {
        "h264:3840x2160:1080p-h264": {"spf": 0.115},   # HW 1080p fails -> CPU x264
        "h264:3840x2160:720p-h264": {"spf": 0.0588},   # HW encode (decode-bound)
        "h264:3840x2160:540p-h264": {"spf": 0.0556},   # HW encode (decode-bound)
        "h264:3840x2160:720p-h265": {"spf": 0.182},    # CPU x265
        "hevc:3840x2160:720p-h264": {"spf": 0.0303},   # HW decode + HW encode
        "hevc:3840x2160:540p-h264": {"spf": 0.0575},   # HW decode + HW encode; decode-bound (17 fps)
        "hevc:3840x2160:1080p-h264": {"spf": 0.0833},  # HW decode + CPU x264 (HW 1080p fails)
        "hevc:3840x2160:720p-h265": {"spf": 0.147},    # HW decode + CPU x265
    },
}


class TranscodeManager:
    """Single-worker transcode job queue backed by ffmpeg."""

    def __init__(self, config: Any, hub: Any, browse: Any, settings: Any = None) -> None:
        if browse is None:
            raise TranscodeError("transcoding requires the file browser (mounts)")
        self._config = config
        self._hub = hub
        self._state = hub.state  # StationState (shared operation_lock + snapshot)
        self._browse = browse
        tc = config.get("transcode", {}) or {}
        self._output_dirname = str(tc.get("output_dirname", "Transcoded"))
        self._presets = list(tc.get("presets", []))
        self._acceleration = str(tc.get("acceleration", "auto"))
        self._fallback_to_cpu = bool(tc.get("fallback_to_cpu", True))
        self._ram_buffer = bool(tc.get("ram_buffer", True))
        self._ram_buffer_fraction = float(tc.get("ram_buffer_fraction", 2 / 3))
        self._work_base = "/run/copystation/transcode-work"
        # Learned wall-time-per-frame per (source codec+resolution, preset), used
        # to estimate a new job's duration. Persisted so it survives restarts.
        self._perf_file = str(tc.get("perf_file", "/var/lib/copystation/transcode-perf.json"))
        # Runtime-mutable settings (default preset + auto-transcode), persisted to
        # an overlay file so a web-UI change survives a restart without rewriting
        # the (commented) config. The overlay, when present, wins over the config.
        # The "transcode" section of the shared user-settings overlay. The daemon
        # passes the shared store's section (so transcode + wifi_ap share ONE file
        # with no cross-writer races); when constructed standalone (tests, the web
        # in simulation) we open our own store at the configured path.
        self._settings = settings if settings is not None else SettingsStore(
            config.get("user_settings_file", DEFAULT_USER_SETTINGS_FILE)
        ).section("transcode")
        self._settings_lock = threading.Lock()
        self._auto_transcode = (
            bool(self._settings.get("auto_transcode"))
            if self._settings.has("auto_transcode")
            else bool(tc.get("auto_transcode", False))
        )
        self._default_preset = self._resolve_default_preset(
            self._settings.get("default_preset")
            if self._settings.has("default_preset")
            else tc.get("default_preset")
        )
        # Where transcoded files land on the source medium ("central" | "same").
        # The overlay (a web-UI change) wins over the config default.
        self._output_location = self._resolve_output_location(
            self._settings.get("output_location")
            if self._settings.has("output_location")
            else tc.get("output_location")
        )
        # Mirror the auto-transcode setting onto the shared state so the e-paper
        # badge and the web header reflect it (and a button toggle updates it live).
        self._state.set_auto_transcode(self._auto_transcode)
        self._available = ffmpeg_available()
        # Detect the board and probe which encoders ffmpeg actually has, once at
        # startup, so hardware acceleration can be picked per job without re-probing.
        self._board = detect_board()
        # Probe both ffmpeg encoders and GStreamer elements: the Cubie's hardware
        # H.264 encoder is a GStreamer OMX element, not an ffmpeg encoder.
        self._encoders_avail = (
            available_encoders() | available_gst_elements() if self._available else set()
        )
        # ffmpeg hardware-acceleration methods, for HEVC decode offload (Pi 5 "drm").
        self._hwaccels = available_hwaccels() if self._available else set()
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._lock = threading.Lock()
        self._perf_lock = threading.Lock()
        self._perf: dict[str, Any] = self._load_perf()
        self._probe_cache: dict[str, Any] = {}  # path -> (stat-sig, probed info)
        self._jobs: dict[int, dict] = {}
        self._order: list[int] = []
        self._seq = 0
        # Jobs finished in the current batch/run, for the queue's overall progress.
        self._run_done = 0
        # Monotonic time the current run started (spans the whole batch), for the
        # queue's overall elapsed time. ``None`` between runs.
        self._run_started: Optional[float] = None
        self._current_proc: Optional[subprocess.Popen] = None
        self._current_id: Optional[int] = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        if not self._available:
            _LOG.warning("Transcoding enabled but ffmpeg/ffprobe not found on PATH.")
        else:
            _LOG.info(
                "Transcoding ready (board=%s, acceleration=%s, fallback_to_cpu=%s)",
                self._board, self._acceleration, self._fallback_to_cpu,
            )

    def _encoders_for(self, preset: dict) -> List[Encoder]:
        """Ordered encoder candidates for a preset (HW first, then CPU fallback).

        A per-preset ``accel`` overrides the global ``transcode.acceleration``.
        """
        accel = str(preset.get("accel") or self._acceleration)
        return select_encoders(
            preset,
            board=self._board,
            available=self._encoders_avail,
            acceleration=accel,
            fallback_to_cpu=self._fallback_to_cpu,
        )

    # ----- introspection --------------------------------------------------------

    def _preset(self, preset_id: str) -> dict:
        for preset in self._presets:
            if str(preset.get("id")) == str(preset_id):
                return preset
        raise UnknownPreset(f"unknown preset {preset_id!r}")

    def _has_active_job(self) -> bool:
        with self._lock:
            return any(j["status"] in ("queued", "running") for j in self._jobs.values())

    # ----- runtime-mutable settings (default preset + auto-transcode) ------------

    def _first_preset_id(self) -> Optional[str]:
        for preset in self._presets:
            pid = preset.get("id")
            if pid is not None:
                return str(pid)
        return None

    def _is_valid_preset(self, preset_id: Any) -> bool:
        return any(str(p.get("id")) == str(preset_id) for p in self._presets)

    def _resolve_default_preset(self, requested: Any) -> Optional[str]:
        """A valid preset id: the requested one if configured, else the first."""
        if requested is not None and self._is_valid_preset(requested):
            return str(requested)
        return self._first_preset_id()

    @staticmethod
    def _resolve_output_location(requested: Any) -> str:
        """A valid output location ("central"/"same"); the default when unset/bad."""
        val = str(requested).strip().lower() if requested is not None else ""
        return val if val in OUTPUT_LOCATIONS else DEFAULT_OUTPUT_LOCATION

    def _output_rel_dir(self, input_path: str) -> str:
        """Volume-relative directory the transcoded file is written into.

        ``central`` -> ``<output_dirname>`` at the volume root; ``same`` -> an
        ``<output_dirname>`` folder inside the source file's own directory (so the
        transcode lands beside its original). The output is always on the source's
        own medium either way.
        """
        if self.output_location == "same":
            parent = PurePosixPath(str(input_path)).parent
            base = "" if parent.as_posix() in (".", "") else parent.as_posix()
            return f"{base}/{self._output_dirname}" if base else self._output_dirname
        return self._output_dirname

    @property
    def default_preset(self) -> Optional[str]:
        """The preset preselected in the web UI dialogs / used by auto-transcode."""
        with self._settings_lock:
            return self._default_preset

    @property
    def output_location(self) -> str:
        """Where transcoded files land on the source medium ("central" | "same")."""
        with self._settings_lock:
            return self._output_location

    @property
    def auto_transcode(self) -> bool:
        """Whether a successful copy auto-queues a transcode of its files."""
        with self._settings_lock:
            return self._auto_transcode

    def set_settings(self, default_preset: Any = None,
                     auto_transcode: Any = None,
                     output_location: Any = None) -> dict:
        """Update and persist the runtime settings; return the current values.

        Only the provided fields change. An unknown ``default_preset`` raises
        :class:`UnknownPreset`; an unsupported ``output_location`` raises
        :class:`InvalidSetting`.
        """
        with self._settings_lock:
            changed: dict[str, Any] = {}
            if default_preset is not None:
                if not self._is_valid_preset(default_preset):
                    raise UnknownPreset(f"unknown preset {default_preset!r}")
                self._default_preset = str(default_preset)
                changed["default_preset"] = self._default_preset
            if auto_transcode is not None:
                self._auto_transcode = bool(auto_transcode)
                changed["auto_transcode"] = self._auto_transcode
            if output_location is not None:
                loc = str(output_location).strip().lower()
                if loc not in OUTPUT_LOCATIONS:
                    raise InvalidSetting(f"unknown output_location {output_location!r}")
                self._output_location = loc
                changed["output_location"] = loc
            if changed:
                self._settings.update(**changed)  # persist only the changed key(s)
            _LOG.info("Transcode settings updated (default_preset=%s, auto_transcode=%s, "
                      "output_location=%s)", self._default_preset, self._auto_transcode,
                      self._output_location)
            result = {"default_preset": self._default_preset,
                      "auto_transcode": self._auto_transcode,
                      "output_location": self._output_location}
        # Reflect the (possibly changed) auto-transcode flag on the shared state
        # for the e-paper badge / web header. Done outside the settings lock.
        self._state.set_auto_transcode(result["auto_transcode"])
        return result

    # ----- probing / planning / performance model -------------------------------

    def _probe(self, src: Path) -> dict:
        """`probe_video_info` with a small cache keyed by path + mtime + size."""
        key = str(src)
        try:
            st = src.stat()
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = None
        with self._lock:
            cached = self._probe_cache.get(key)
            if cached is not None and cached[0] == sig:
                return cached[1]
        info = probe_video_info(src)
        with self._lock:
            self._probe_cache[key] = (sig, info)
        return info

    def _plan(self, preset: dict, info: dict) -> tuple:
        """Which path a (preset, source) will take: ``hw`` / ``hw+cpu`` / ``cpu``.

        Returns ``(path, produced_height)``: ``hw`` is a single hardware pass,
        ``hw+cpu`` a hardware decode-scale plus a CPU finishing pass, ``cpu`` a pure
        software transcode.
        """
        encoders = self._encoders_for(preset)
        first = encoders[0] if encoders else None
        if first is not None and first.is_gstreamer and gst_can_handle(info):
            target_h = int(preset.get("height", 0) or 0)
            out_h = gstreamer_output_height(int(info.get("height") or 0), target_h)
            if target_h > 0 and out_h > 0 and out_h != target_h:
                return "hw+cpu", out_h
            return "hw", out_h
        return "cpu", int(preset.get("height", 0) or 0)

    def plan_for(self, input_device: str, input_path: str, preset_id: str) -> dict:
        """File properties + predicted path + duration estimate for the web dialog."""
        if not self._available:
            raise TranscodeUnavailable("ffmpeg is not installed on the station")
        preset = self._preset(preset_id)                              # -> UnknownPreset
        src = self._browse.resolve_input(input_device, input_path)    # -> BrowseError
        info = dict(self._probe(src))
        try:
            info["size"] = src.stat().st_size
        except OSError:
            info["size"] = None
        path_kind, out_h = self._plan(preset, info)
        keep = ("vcodec", "width", "height", "fps", "duration",
                "has_audio", "acodec", "container", "size")
        return {
            "preset": preset_id,
            "info": {k: info.get(k) for k in keep},
            "path": path_kind,
            "out_height": out_h,
            "target_height": int(preset.get("height", 0) or 0),
            "estimate_seconds": self._estimate(info, preset_id),
        }

    def plan_folder(self, input_device: str, input_path: str, preset_id: str) -> dict:
        """Per-file plan for every video in a folder, for the batch dialog.

        Lists the folder (non-recursive), probes each video file and predicts which
        path (``hw`` / ``hw+cpu`` / ``cpu``) the chosen preset takes for it, so the
        dialog can show up front whether the files are handled uniformly or split
        across the hardware and CPU encoders. Submitting creates one job per file.
        """
        if not self._available:
            raise TranscodeUnavailable("ffmpeg is not installed on the station")
        preset = self._preset(preset_id)                            # -> UnknownPreset
        listing = self._browse.list_dir(input_device, input_path)   # -> BrowseError
        folder = listing.get("path", "")
        files: List[dict] = []
        counts: dict[str, int] = {"hw": 0, "hw+cpu": 0, "cpu": 0}
        for entry in listing.get("entries", []):
            name = entry.get("name", "")
            if entry.get("is_dir") or not is_video_file(name):
                continue
            rel = f"{folder}/{name}" if folder else name
            try:
                src = self._browse.resolve_input(input_device, rel)
                info = dict(self._probe(src))
            except BrowseError:  # a file vanished mid-listing -> skip it
                continue
            path_kind, out_h = self._plan(preset, info)
            counts[path_kind] = counts.get(path_kind, 0) + 1
            files.append({
                "name": name,
                "path": rel,
                "vcodec": info.get("vcodec"),
                "width": info.get("width"),
                "height": info.get("height"),
                "fps": info.get("fps"),
                "duration": info.get("duration"),
                "size": entry.get("size"),
                "plan": path_kind,
                "out_height": out_h,
                "estimate_seconds": self._estimate(info, preset_id),
            })
        total = sum(f["estimate_seconds"] for f in files if f["estimate_seconds"])
        return {
            "preset": preset_id,
            "folder": folder,
            "target_height": int(preset.get("height", 0) or 0),
            "count": len(files),
            "counts": counts,
            "files": files,
            "estimate_seconds": total or None,
        }

    def _load_perf(self) -> dict:
        try:
            data = json.loads(Path(self._perf_file).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_perf(self) -> None:
        try:
            path = Path(self._perf_file)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._perf, indent=2, sort_keys=True))
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - best effort
            _LOG.warning("Could not persist transcode perf model: %s", exc)

    def _estimate(self, info: dict, preset_id: str) -> Optional[float]:
        key = perf_key(info, preset_id)
        with self._perf_lock:
            entry = self._perf.get(key)
        if not isinstance(entry, dict):
            # Fall back to the built-in seed for THIS board only (the numbers are
            # hardware-specific; a Pi must not use the Cubie's).
            entry = DEFAULT_PERF.get(self._board, {}).get(key)
        spf = entry.get("spf") if isinstance(entry, dict) else None
        return estimate_seconds(spf, info.get("duration"), info.get("fps"))

    def _update_perf(self, info: dict, preset_id: str, wall_seconds: float) -> None:
        """Train the model from a COMPLETED job's total wall time over its frames."""
        duration, fps = info.get("duration"), info.get("fps")
        if not duration or not fps or duration <= 0 or fps <= 0 or wall_seconds <= 0:
            return
        frames = duration * fps
        self._record_perf(info, preset_id, wall_seconds / frames,
                          wall=wall_seconds, frames=int(frames))

    def _record_perf(self, info: dict, preset_id: str, spf: float,
                     wall: Optional[float] = None, frames: Optional[int] = None) -> None:
        """Compare a measured seconds-per-frame with the stored value for this
        source+preset and overwrite it on a notable deviation (>15%).

        Shared by completed jobs (spf from the total wall time) and canceled jobs
        (spf tracked live once it stabilised), so a long-enough canceled job still
        refreshes the estimate. Small run-to-run noise is ignored so the model does
        not churn.
        """
        if not spf or spf <= 0:
            return
        if not (info.get("vcodec") and info.get("width") and info.get("height")):
            return  # incomplete source info -> unusable key
        key = perf_key(info, preset_id)
        with self._perf_lock:
            cur = (self._perf.get(key) or {}).get("spf")
            if cur and abs(spf - cur) / cur <= 0.15:
                return
            entry: dict = {"spf": spf}
            if wall is not None:
                entry["wall"] = round(wall, 1)
            if frames is not None:
                entry["frames"] = frames
            self._perf[key] = entry
        self._save_perf()

    # ----- per-job duration estimates + queue aggregate -------------------------

    def _estimate_via_browse(self, device: str, rel: str, preset_id: str) -> Optional[float]:
        """Best-effort duration estimate of ``device:rel`` (read-only mount)."""
        try:
            src = self._browse.resolve_input(device, rel)
            return self._estimate(self._probe(src), preset_id)
        except Exception:  # pragma: no cover - estimate is best-effort
            return None

    def _estimate_for_path(self, mount_root: Any, rel: str,
                           preset_id: str) -> Optional[float]:
        """Best-effort estimate from an already-mounted path (no browse mount).

        Used by auto-transcode, which is called by the daemon while the target is
        still mounted at ``mount_root`` -- probing that path directly avoids a
        second (read-only) mount of a device the daemon holds read-write.
        """
        try:
            return self._estimate(self._probe(Path(mount_root) / rel), preset_id)
        except Exception:  # pragma: no cover - estimate is best-effort
            return None

    def _queue_aggregate_locked(self, now: float) -> dict:
        """Queue-wide progress (assumes ``self._lock`` is held).

        ``pending`` counts queued+running jobs; ``count``/``index`` frame it as
        "job index of count" within the current run; ``percent`` is a count-based
        overall fraction (robust without estimates); ``elapsed_seconds`` is the wall
        time since the run started (across all its files, not just the current one);
        ``eta_seconds`` sums the remaining time (the running job's live ETA + the
        queued jobs' estimates), ``None`` when nothing could be estimated.
        """
        run_done = self._run_done
        running: Optional[dict] = None
        queued: list[dict] = []
        for i in self._order:
            j = self._jobs.get(i)
            if j is None:
                continue
            if j["status"] == "running":
                running = j
            elif j["status"] == "queued":
                queued.append(j)
        pending = (1 if running is not None else 0) + len(queued)
        if pending == 0:
            return {"pending": 0, "index": 0, "count": run_done,
                    "percent": 0.0, "elapsed_seconds": None, "eta_seconds": None}
        count = run_done + pending
        run_frac = 0.0
        if running is not None:
            run_frac = max(0.0, min(1.0, float(running.get("percent") or 0) / 100.0))
        percent = round((run_done + run_frac) / count * 100.0, 1) if count else 0.0
        # Remaining wall time across the WHOLE queue, not just the running job. The
        # running job's remaining is its live ETA; each queued job uses its own
        # learned estimate, or -- when it has none (its source key was never seeded
        # nor learned) -- a fallback, so the total still scales with the number of
        # pending jobs. The fallback is the running job's projected full time (a
        # batch is usually homogeneous), else the mean of the estimates we do have.
        running_remaining: Optional[float] = None
        running_full: Optional[float] = None
        if running is not None:
            pj = self._public_job(running, now)
            elapsed, remaining = pj.get("elapsed_seconds"), pj.get("eta_seconds")
            running_remaining = (remaining if remaining is not None
                                 else running.get("estimate_seconds"))
            if elapsed is not None and remaining is not None:
                running_full = elapsed + remaining
            elif running.get("estimate_seconds"):
                running_full = running.get("estimate_seconds")
        known_ests = [j["estimate_seconds"] for j in queued if j.get("estimate_seconds")]
        if running is not None and running.get("estimate_seconds"):
            known_ests.append(running["estimate_seconds"])
        per_job_fallback = running_full or (
            sum(known_ests) / len(known_ests) if known_ests else None)
        eta = 0.0
        known = False
        if running_remaining is not None:
            eta += running_remaining
            known = True
        for j in queued:
            est = j.get("estimate_seconds")
            if est is None:
                est = per_job_fallback
            if est is not None:
                eta += est
                known = True
        # Wall time the whole run has been going (spans every file, so it keeps
        # climbing between jobs instead of resetting to each file's own elapsed).
        elapsed = (now - self._run_started) if self._run_started is not None else None
        return {"pending": pending, "index": run_done + 1, "count": count,
                "percent": percent,
                "elapsed_seconds": round(elapsed, 1) if elapsed is not None else None,
                "eta_seconds": round(eta, 1) if known else None}

    def _push_queue_state(self) -> None:
        """Mirror the queue aggregate into StationState (for the e-paper panel)."""
        now = time.monotonic()
        with self._lock:
            q = self._queue_aggregate_locked(now)
        self._state.set_transcode_queue(
            pending=q["pending"], index=q["index"], count=q["count"],
            eta_seconds=q["eta_seconds"], percent=q["percent"],
            elapsed_seconds=q["elapsed_seconds"],
        )

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            jobs = [self._public_job(self._jobs[i], now) for i in reversed(self._order)]
            queue = self._queue_aggregate_locked(now)
        with self._settings_lock:
            default_preset = self._default_preset
            auto_transcode = self._auto_transcode
            output_location = self._output_location
        return {
            "available": self._available,
            "output_dirname": self._output_dirname,
            "output_location": output_location,
            "board": self._board,
            "acceleration": self._acceleration,
            "default_preset": default_preset,
            "auto_transcode": auto_transcode,
            "presets": [
                {"id": p.get("id"), "label": p.get("label", p.get("id"))}
                for p in self._presets
            ],
            "queue": queue,
            "jobs": jobs,
        }

    @staticmethod
    def _public_job(job: dict, now: float) -> dict:
        """Copy of a job for the API, with live elapsed/ETA for a running one."""
        j = dict(job)
        started = j.pop("started", None)
        j.pop("perf_spf", None)      # internal tracking, not part of the API
        j.pop("perf_stable", None)
        j["elapsed_seconds"] = None
        j["eta_seconds"] = None
        if j["status"] == "running" and started is not None:
            elapsed = max(0.0, now - started)
            j["elapsed_seconds"] = round(elapsed, 1)
            percent = float(j.get("percent") or 0)
            if percent > 0:
                j["eta_seconds"] = round(max(0.0, elapsed * (100 - percent) / percent), 1)
        return j

    # ----- submission -----------------------------------------------------------

    @staticmethod
    def _new_job(job_id: int, input_device: str, input_path: str,
                 output_device: str, preset_id: str) -> dict:
        """A fresh queued-job record (shared by single and batch submission)."""
        return {
            "id": job_id,
            "status": "queued",
            "input_device": input_device,
            "input_path": input_path,
            "output_device": output_device,
            "preset": str(preset_id),
            "percent": 0,
            "filename": None,
            "output_path": None,
            "encoder": None,       # which encoder actually ran (cpu / h264_v4l2m2m ...)
            "hw": False,           # True if a hardware encoder was used
            "path": None,          # "hw" | "hw+cpu" | "cpu" (chosen encode path)
            "perf_spf": None,      # internal: live seconds-per-frame estimate
            "perf_stable": False,  # internal: True once that estimate stabilised
            "input_size": None,    # source file size in bytes
            "output_size": None,   # transcoded file size in bytes (once done)
            "estimate_seconds": None,  # predicted wall time (from the perf model)
            "ram_buffered": False, # True if staged through a RAM tmpfs
            "fps": None,           # live encode rate (frames/second)
            "speed": None,         # ffmpeg speed relative to realtime (e.g. "2.5x")
            "error": None,
            "started": None,       # wall clock (time.monotonic) when it starts running
        }

    def submit(
        self,
        input_device: str,
        input_path: str,
        preset_id: str,
    ) -> dict:
        if not self._available:
            raise TranscodeUnavailable("ffmpeg is not installed on the station")
        self._preset(preset_id)  # validate early -> UnknownPreset
        # Copy and transcode are mutually exclusive, and only one transcode runs at
        # a time: refuse up front rather than silently queueing behind a copy.
        if self._state.phase is State.COPYING:
            raise TranscodeBusy("a copy is in progress -- try again once it finishes")
        if self._has_active_job():
            raise TranscodeBusy("a transcode is already in progress")
        # Validate the input up front (mounts read-only) so a bad file/volume is
        # reported immediately instead of failing asynchronously.
        self._browse.resolve_input(input_device, input_path)
        # The transcode is always written to the SOURCE file's own medium.
        out_dev = input_device
        with self._lock:
            self._seq += 1
            job_id = self._seq
            job = self._new_job(job_id, input_device, input_path, out_dev, preset_id)
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._trim_history()
            snap = dict(job)
        self._queue.put(job_id)
        self._ensure_worker()
        _LOG.info("Transcode queued (#%d): %s:%s -> %s [%s]",
                  job_id, input_device, input_path, out_dev, preset_id)
        return snap

    def submit_folder(
        self,
        input_device: str,
        input_path: str,
        preset_id: str,
    ) -> dict:
        """Queue one independent job per video file in a folder (single preset).

        A folder is *not* one 'folder job': each file becomes its own job that the
        single worker runs one after another (so each picks its own hw/cpu path and
        appears/cancels individually). Like :meth:`submit`, it refuses if a copy or
        another transcode is already active, so the batch owns the queue.
        """
        if not self._available:
            raise TranscodeUnavailable("ffmpeg is not installed on the station")
        self._preset(preset_id)  # validate early -> UnknownPreset
        if self._state.phase is State.COPYING:
            raise TranscodeBusy("a copy is in progress -- try again once it finishes")
        if self._has_active_job():
            raise TranscodeBusy("a transcode is already in progress")
        listing = self._browse.list_dir(input_device, input_path)  # -> BrowseError
        folder = listing.get("path", "")
        names = [e.get("name", "") for e in listing.get("entries", [])
                 if not e.get("is_dir") and is_video_file(e.get("name", ""))]
        if not names:
            raise TranscodeError("no video files in this folder")
        out_dev = input_device  # always written to the source's own medium
        rels = [f"{folder}/{name}" if folder else name for name in names]
        estimates = {rel: self._estimate_via_browse(input_device, rel, preset_id)
                     for rel in rels}
        snaps: List[dict] = []
        with self._lock:
            for rel in rels:
                self._seq += 1
                job_id = self._seq
                job = self._new_job(job_id, input_device, rel, out_dev, preset_id)
                job["estimate_seconds"] = estimates.get(rel)
                self._jobs[job_id] = job
                self._order.append(job_id)
                snaps.append(dict(job))
            self._trim_history()
        for snap in snaps:
            self._queue.put(snap["id"])
        self._ensure_worker()
        _LOG.info("Transcode batch queued (%d files): %s:%s -> %s [%s]",
                  len(snaps), input_device, folder or "/", out_dev, preset_id)
        return {"jobs": snaps, "count": len(snaps)}

    def submit_auto(
        self,
        input_device: str,
        rels: List[str],
        mount_root: Any = None,
        preset_id: Optional[str] = None,
    ) -> dict:
        """Queue one job per already-listed file, for auto-transcode after a copy.

        Called by the copy daemon while it still holds ``operation_lock`` (phase
        SUCCESS, no active job), so -- unlike :meth:`submit`/:meth:`submit_folder`
        -- it does not refuse on busy state; it just enqueues. ``rels`` are volume-
        relative paths the caller already filtered to videos. ``mount_root`` (the
        daemon's target mountpoint) is used only to estimate each job's duration
        without a second mount of the device. ``preset_id`` defaults to the
        persisted :attr:`default_preset`.
        """
        if not self._available:
            raise TranscodeUnavailable("ffmpeg is not installed on the station")
        preset = preset_id or self.default_preset
        if not preset:
            raise TranscodeError("no transcode preset configured")
        self._preset(preset)  # validate -> UnknownPreset
        out_dev = input_device  # always written to the source's own medium
        estimates = ({rel: self._estimate_for_path(mount_root, rel, preset)
                      for rel in rels} if mount_root is not None else {})
        snaps: List[dict] = []
        with self._lock:
            for rel in rels:
                self._seq += 1
                job_id = self._seq
                job = self._new_job(job_id, input_device, rel, out_dev, preset)
                job["estimate_seconds"] = estimates.get(rel)
                self._jobs[job_id] = job
                self._order.append(job_id)
                snaps.append(dict(job))
            self._trim_history()
        for snap in snaps:
            self._queue.put(snap["id"])
        self._ensure_worker()
        _LOG.info("Auto-transcode batch queued (%d files): %s -> %s [%s]",
                  len(snaps), input_device, out_dev, preset)
        return {"jobs": snaps, "count": len(snaps), "preset": preset}

    def cancel(self, job_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job["status"] in ("done", "error", "canceled"):
                return False
            job["status"] = "canceled"
            proc = self._current_proc if self._current_id == job_id else None
        if proc is not None:
            proc.terminate()
        return True

    # ----- worker ---------------------------------------------------------------

    def _ensure_worker(self) -> None:
        if self._worker is not None:
            return
        self._worker = threading.Thread(
            target=self._run_worker, name="copystation-transcode", daemon=True
        )
        self._worker.start()

    def _run_worker(self) -> None:
        while not self._stop.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._run_batch(job_id)

    def _prev_phase_for_run(self) -> State:
        """The phase to restore when the batch finishes (never TRANSCODING)."""
        prev = self._state.phase
        return State.READY if prev is State.TRANSCODING else prev

    def _finish_run(self, prev_phase: State, last_error: Optional[str]) -> None:
        """End a run: surface the last error (ERROR phase) or restore ``prev_phase``."""
        with self._lock:
            self._run_done = 0
            self._run_started = None
        if last_error is not None:
            self._hub.fail_transcode(f"Transcode failed: {last_error}")
        else:
            self._hub.finish_transcode(prev_phase)

    def _run_batch(self, first_job_id: int) -> None:
        """Run a whole queue drain as ONE operation under a single lock + phase.

        Holding ``operation_lock`` and the TRANSCODING phase across every queued
        file (rather than per file) keeps the display stable for a multi-file
        batch -- the phase does not flash between files -- and closes the gap where
        the copy daemon could otherwise mount a card between two transcode files.
        A per-file failure/cancel is recorded on its job and the batch continues;
        the phase is restored (or ERROR shown) once the queue drains.
        """
        with self._state.operation_lock:
            prev_phase = self._prev_phase_for_run()
            with self._lock:
                self._run_done = 0
                self._run_started = time.monotonic()
            last_error: Optional[str] = None
            job_id = first_job_id
            while True:
                err = self._process_one(job_id)
                if err is not None:
                    last_error = err
                with self._lock:
                    self._run_done += 1
                self._push_queue_state()  # reflect the advanced run position
                try:
                    job_id = self._queue.get_nowait()
                except queue.Empty:
                    break
            self._finish_run(prev_phase, last_error)

    def _process(self, job_id: int) -> None:
        """Run a single job as a one-item batch (retained for tests)."""
        with self._state.operation_lock:
            prev_phase = self._prev_phase_for_run()
            with self._lock:
                self._run_done = 0
                self._run_started = time.monotonic()
            err = self._process_one(job_id)
            with self._lock:
                self._run_done += 1
            self._finish_run(prev_phase, err)

    def _process_one(self, job_id: int) -> Optional[str]:
        """Encode one queued job. Returns an error message, or ``None`` on
        success/cancel/skip. Does NOT take ``operation_lock`` or change the phase
        (the batch owns both) -- only marks the job and updates the queue state.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] != "queued":
                return None  # canceled before it started, or already handled
            input_device = job["input_device"]
            input_path = job["input_path"]
            preset_id = job["preset"]

        started = time.monotonic()
        self._set(job_id, status="running", started=started)
        out_dir_rel = self._output_rel_dir(input_path)  # central vs same folder
        out_name = output_name(Path(input_path).name, preset_id)
        out_rel = f"{out_dir_rel}/{out_name}"
        self._set(job_id, filename=out_name, output_path=out_rel)
        # Update the current-file name on every backend (phase already TRANSCODING).
        self._hub.begin_transcode(Path(input_path).name)
        self._push_queue_state()
        # Source media info, captured while the volume is still mounted so the perf
        # model can be trained even from the cancel handler below (which runs after
        # the ``finally`` has unmounted the volume -- a re-probe there would fail).
        src_info: Optional[dict] = None
        try:
            # The transcode reads its source and writes its output on the SAME
            # (source) medium, so mount it once read-write. Drop any read-only
            # browse mount of it first: a block device can't be mounted read-only
            # and read-write at once (shared superblock -> the read-only one wins).
            # Under the operation lock the daemon holds no mount of it either.
            self._browse.release(input_device)
            root = self._browse.mount_rw(input_device)
            try:
                src = safe_resolve(root, input_path)
                if not src.is_file():
                    raise NotFound(f"{input_path!r} is not a file")
                try:
                    input_size = src.stat().st_size
                except OSError:
                    input_size = 0
                self._set(job_id, input_size=input_size)
                self._state.set_transcode_meta(input_size=input_size)
                # Probe now (still mounted); reused for the perf model on both
                # completion and cancel, and warms the cache for the encode.
                src_info = self._probe(src)
                out_dir = root / out_dir_rel
                out_dir.mkdir(parents=True, exist_ok=True)
                # Never clobber an existing output (e.g. two sources in a batch
                # that map to the same name): fall back to <stem>_2, _3, ...
                dst = unique_output_path(out_dir / out_name)
                if dst.name != out_name:
                    out_name = dst.name
                    out_rel = f"{out_dir_rel}/{out_name}"
                    self._set(job_id, filename=out_name, output_path=out_rel)
                self._encode(job_id, src, dst, self._preset(preset_id))
                try:
                    self._set(job_id, output_size=dst.stat().st_size)
                except OSError:  # pragma: no cover - defensive
                    pass
                # Learn the wall-time-per-frame so future jobs can be estimated.
                try:
                    self._update_perf(src_info, preset_id, time.monotonic() - started)
                except Exception:  # pragma: no cover - perf is best-effort
                    pass
                subprocess.run(["sync"], check=False)
            finally:
                self._browse.umount_rw(input_device)
            with self._lock:
                job = self._jobs.get(job_id)
                if job is not None and job["status"] == "running":
                    job["status"] = "done"
                    job["percent"] = 100
            _LOG.info("Transcode #%d done -> %s:%s", job_id, input_device, out_rel)
            return None
        except _Canceled:
            self._set(job_id, status="canceled")
            # A single-stage job that ran long enough still trains the estimate
            # model (its live seconds-per-frame had stabilised before the abort).
            with self._lock:
                j = self._jobs.get(job_id) or {}
                spf = j.get("perf_spf") if j.get("perf_stable") else None
            if spf and src_info:
                try:
                    self._record_perf(src_info, preset_id, spf)
                    _LOG.info("Transcode #%d canceled -- kept a stable perf sample", job_id)
                except Exception:  # pragma: no cover - perf is best-effort
                    pass
            _LOG.info("Transcode #%d canceled", job_id)
            return None
        except (BrowseError, TranscodeError) as exc:
            self._fail(job_id, exc)
            # A cancel that raced with a failure keeps the canceled status and is
            # not surfaced as a batch error.
            return None if self._is_canceled(job_id) else str(exc)
        except Exception as exc:  # pragma: no cover - defensive
            self._fail(job_id, exc, crash=True)
            return None if self._is_canceled(job_id) else str(exc)

    def _set(self, job_id: int, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _ram_budget(self) -> int:
        """RAM the buffer may use this job (0 disables buffering)."""
        if not self._ram_buffer:
            return 0
        return ram_budget(mem_available_bytes(), self._ram_buffer_fraction)

    def _encode(self, job_id: int, src: Path, final_dst: Path, preset: dict) -> None:
        """Encode ``src`` to ``final_dst``, buffering the OUTPUT through RAM.

        Only the output is staged in a size-capped tmpfs: the input **streams from
        the card** (sequential reads are card-friendly), the encode writes into
        RAM, and the finished file is copied back to the card in one bulk write --
        so the card is never read and written at the same time. Because the input
        is not held in RAM, its size is irrelevant; even large inputs are buffered
        as long as the (usually much smaller) output fits the budget. Falls back to
        streaming straight to the card when the output would not fit (or the
        duration is unknown, or buffering is disabled).
        """
        budget = self._ram_budget()
        duration = probe_duration(src)
        est_out = estimate_output_bytes(duration, preset)

        if budget <= 0 or est_out <= 0 or est_out > budget:
            if budget > 0 and est_out > budget:
                _LOG.info(
                    "Transcode #%d: est. output %d MB exceeds the RAM budget %d MB "
                    "-- streaming to the card.",
                    job_id, est_out // (1024 * 1024), budget // (1024 * 1024),
                )
            self._set(job_id, ram_buffered=False)
            self._encode_with_fallback(job_id, src, final_dst, preset, duration=duration)
            return

        self._set(job_id, ram_buffered=True)
        work = Path(self._work_base) / f"job{job_id}"
        self._mount_tmpfs(work, budget)
        try:
            tmp_out = work / final_dst.name
            _LOG.info(
                "Transcode #%d: buffering output in RAM (tmpfs cap %d MB, est output %d MB)",
                job_id, budget // (1024 * 1024), est_out // (1024 * 1024),
            )
            # Input streams from the card; output is written into RAM.
            self._encode_with_fallback(job_id, src, tmp_out, preset, duration=duration)
            shutil.copy2(tmp_out, final_dst)          # bulk write: RAM -> card
        finally:
            self._umount_tmpfs(work)

    def _mount_tmpfs(self, path: Path, size_bytes: int) -> None:
        """Mount a size-capped tmpfs at ``path`` (isolated so tests can bypass it)."""
        path.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["mount", "-t", "tmpfs", "-o",
                 f"size={size_bytes},nosuid,nodev,noexec", "tmpfs", str(path)],
                capture_output=True, text=True, check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TranscodeError(f"could not mount RAM buffer: {exc}") from exc

    def _umount_tmpfs(self, path: Path) -> None:
        subprocess.run(["umount", str(path)], capture_output=True, check=False)
        try:
            path.rmdir()
        except OSError:  # pragma: no cover - best effort
            pass

    def _encode_with_fallback(self, job_id: int, src: Path, dst: Path, preset: dict,
                              duration: Optional[float] = None) -> None:
        """Try each candidate encoder in turn; fall back to the next on failure.

        Hardware first (when configured/available), then software. A hardware
        encode that fails at runtime (missing device node, unsupported input,
        driver error) does not abort the job -- the partial output is removed and
        the next candidate (ultimately the CPU) is tried. A cancel aborts without
        falling back. ``duration`` (seconds) may be passed to avoid a second
        ffprobe when the caller already measured it.
        """
        if duration is None:
            duration = probe_duration(src)
        encoders = self._encoders_for(preset)
        info = self._probe(src)          # source media info (codec/dims/fps/duration)
        # Offload the source decode to hardware when the board supports it for this
        # codec (Pi 5: HEVC via -hwaccel drm). This prepends a hardware-decode
        # variant of the software encoder, keeping the plain-decode one right behind
        # it as the automatic fallback. Skipped when the encoder is forced to the
        # CPU (acceleration cpu/software/none), which means "no hardware at all".
        accel = str(preset.get("accel") or self._acceleration).strip().lower()
        if accel not in ("cpu", "software", "none"):
            encoders = with_decode_offload(
                encoders, self._board, info.get("vcodec"), self._hwaccels)
        src_fps = info.get("fps")        # for the live perf estimate (single-stage)
        last_exc: Optional[TranscodeError] = None
        for idx, enc in enumerate(encoders):
            if enc.is_gstreamer:
                if not gst_can_handle(info):
                    _LOG.info(
                        "Transcode #%d: %s cannot handle this source "
                        "(codec=%s %sx%s container=%s audio=%s) -- skipping to next",
                        job_id, enc.name, info.get("vcodec"), info.get("width"),
                        info.get("height"), info.get("container"),
                        info.get("acodec") if info.get("has_audio") else "none",
                    )
                    continue
            # Surface a hardware-decode offload in the encoder label (e.g.
            # "cpu (hevc hw-decode)") without changing the encoder's identity: it is
            # still the CPU codec, so ``hw`` (hardware *encoder*) stays False.
            disp = _encoder_label(enc)
            self._set(job_id, encoder=disp, hw=enc.is_hardware, percent=0)
            # Record the encoder on the shared state too (shown on the display).
            self._hub.set_transcode_progress(0.0, disp, enc.is_hardware)
            _LOG.info("Transcode #%d: encoding with %s (%s, %s%s)",
                      job_id, enc.name, enc.kind, enc.engine,
                      f", hwaccel {enc.decode_hwaccel}" if enc.decode_hwaccel else "")
            try:
                if enc.is_gstreamer:
                    self._run_gstreamer(job_id, src, dst, preset, info, enc, duration)
                else:
                    self._set(job_id, path="cpu")
                    self._run_encode(job_id, build_ffmpeg_cmd(enc, preset, src, dst),
                                     duration, "ffmpeg", src_fps=src_fps, track_perf=True)
                return
            except _Canceled:
                _cleanup(dst)
                raise
            except TranscodeError as exc:
                last_exc = exc
                _cleanup(dst)
                has_more = idx + 1 < len(encoders)
                if has_more:
                    nxt = _encoder_label(encoders[idx + 1])
                    _LOG.warning(
                        "Transcode #%d: encoder %s failed (%s) -- falling back to %s",
                        job_id, disp, exc, nxt,
                    )
                    self._set(job_id, note=f"{disp} failed, retrying with {nxt}")
                    continue
                raise
        # Unreachable (the loop always returns or raises), but be explicit.
        raise last_exc or TranscodeError("no encoder available")

    def _run_gstreamer(self, job_id: int, src: Path, dst: Path, preset: dict,
                       info: dict, enc: Encoder, duration: Optional[float]) -> None:
        """Hardware GStreamer encode, with an optional CPU finishing pass.

        The decoder downscales by 1/2 or 1/4 (artifact-free); when the requested
        height is not exactly a 1/2-step of the source, the hardware stage produces
        the nearest larger clean size and a light ffmpeg pass scales it down to the
        exact target on the CPU. The encoder scaler (which leaves a magenta bottom
        line) is never used. Raising propagates to the caller's CPU fallback.

        The Allwinner OMX **HEVC** encoder additionally pads the coded picture up to a
        coding-block multiple (e.g. 1080 -> 1088) and emits no conformance window --
        rendered as a magenta bottom strip. So for ``omxhevcvideoenc`` output only, a
        single hardware pass is followed by a ``-c copy`` remux that rewrites the SPS
        crop (no re-encode), and a two-stage job crops those padding rows off the
        intermediate before the CPU downscale. The H.264 encoder crops correctly and is
        left untouched; the ffmpeg encoders on the Pi never reach this GStreamer path.
        """
        src_fps = info.get("fps")
        target_h = int(preset.get("height", 0) or 0)
        src_w, src_h = int(info.get("width") or 0), int(info.get("height") or 0)
        scale = decoder_scale_factor(src_h, target_h)
        out_h = (src_h >> scale) if src_h > 0 else target_h
        out_w = (src_w >> scale) if src_w > 0 else 0
        is_hevc_hw = enc.codec == "omxhevcvideoenc"

        # Single hardware pass: the decoder lands exactly on the target height.
        if not (target_h > 0 and out_h > 0 and out_h != target_h):
            self._set(job_id, path="hw")
            if not is_hevc_hw:
                self._run_encode(job_id, build_gstreamer_cmd(enc, preset, src, dst, info),
                                 duration, "gstreamer", src_fps=src_fps, track_perf=True)
                return
            # HEVC: encode to a temp, then add the conformance-window crop the encoder
            # omits via a no-re-encode remux (bar 0-90% HW, 90-100% remux).
            stage = dst.parent / f"{dst.stem}.hw{dst.suffix}"
            try:
                self._run_encode(job_id, build_gstreamer_cmd(enc, preset, src, stage, info),
                                 duration, "gstreamer", src_fps=src_fps,
                                 pct_base=0.0, pct_span=90.0)
                crop_r, crop_b = hevc_conformance_crop(*probe_coded_dims(stage), out_w, out_h)
                if crop_r or crop_b:
                    self._set(job_id, note=f"fixing HEVC crop -> {out_w}x{out_h}")
                    self._run_encode(
                        job_id, build_hevc_crop_remux_cmd(stage, dst, crop_r, crop_b),
                        duration, "ffmpeg", pct_base=90.0, pct_span=10.0)
                else:
                    stage.replace(dst)  # already aligned -> nothing to crop
            finally:
                _cleanup(stage)          # no-op once renamed onto dst
            return
        # Two-stage: hardware decode-scale to out_h (bar 0-50%), then a CPU scale to
        # target_h (bar 50-100%). The finish is CRF (quality-controlled): the
        # bitrate-limited hardware encoder is the quality bottleneck, so the CPU
        # finish encodes to a quality target (preset ``crf``) rather than a capped
        # bitrate. The hardware intermediate is a generous bitrate (see
        # default_bitrate) so it preserves detail for the finish.
        stage1 = dst.parent / f"{dst.stem}.stage1.mp4"
        _LOG.info("Transcode #%d: HW %dp, then CPU finish -> %dp", job_id, out_h, target_h)
        self._set(job_id, path="hw+cpu", note=f"pass 1/2: HW {out_h}p")
        try:
            self._run_encode(job_id, build_gstreamer_cmd(enc, preset, src, stage1, info),
                             duration, "gstreamer", src_fps=src_fps,
                             pct_base=0.0, pct_span=50.0)
            self._set(job_id, encoder=f"{enc.name}+cpu", note=f"pass 2/2: CPU {target_h}p")
            # For the HEVC hardware encoder, crop its uncropped padding rows off the
            # intermediate before the CPU downscale (it emits no conformance window);
            # this also yields the exact target width. The H.264 intermediate is already
            # cropped, so it keeps the plain finish.
            finish_pre = [f"crop=iw:{out_h}:0:0"] if is_hevc_hw and out_h > 0 else None
            self._run_encode(job_id, build_ffmpeg_cmd(
                cpu_encoder(str(preset.get("vcodec", "libx264"))),  # CRF (quality)
                preset, stage1, dst, pre_filters=finish_pre),
                duration, "ffmpeg", pct_base=50.0, pct_span=50.0)
        finally:
            _cleanup(stage1)

    def _run_encode(self, job_id: int, cmd: List[str], duration: Optional[float],
                    engine: str = "ffmpeg", src_fps: Optional[float] = None,
                    pct_base: float = 0.0, pct_span: float = 100.0,
                    track_perf: bool = False) -> None:
        """Run one encoder subprocess, tracking progress and honouring cancel.

        ``engine`` selects how progress is read: ffmpeg's ``-progress`` stream, or
        a GStreamer ``progressreport``. ffmpeg reports its own fps/speed; GStreamer
        does not, so for the GStreamer path speed is derived from the output
        position over the elapsed wall time and fps from ``src_fps`` * speed. A
        GStreamer job also gets a stall watchdog (the OMX stack can wedge; see
        ``GST_STALL_SECONDS``). GStreamer's stderr is merged into stdout so the
        ``progressreport`` lines are captured regardless of the chatty OMX output.

        ``pct_base``/``pct_span`` map this stage's 0-100% onto a sub-range of the
        job's bar, so a two-pass job fills 0-50% then 50-100% (not twice to 100%).

        ``track_perf`` (single-stage encodes only) records a live seconds-per-frame
        estimate on the job -- from the job's wall time over the output produced --
        flagged stable once enough output exists, so a canceled job can still train
        the estimate model. It needs ``src_fps``.
        """
        is_gst = engine == "gstreamer"
        stderr = subprocess.STDOUT if is_gst else subprocess.DEVNULL
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=stderr, text=True
            )
        except OSError as exc:
            raise TranscodeError(f"{engine} could not start: {exc}") from exc

        with self._lock:
            self._current_proc = proc
            self._current_id = job_id

        last_line = [time.monotonic()]
        wd_stop = threading.Event()
        watchdog: Optional[threading.Thread] = None
        if is_gst:
            def _watch() -> None:
                while not wd_stop.wait(5.0):
                    if time.monotonic() - last_line[0] > GST_STALL_SECONDS:
                        _LOG.warning(
                            "Transcode #%d: GStreamer produced no output for %ds -- killing",
                            job_id, GST_STALL_SECONDS,
                        )
                        proc.terminate()
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        return
            watchdog = threading.Thread(
                target=_watch, name="copystation-gst-watchdog", daemon=True
            )
            watchdog.start()

        def _emit(raw_pct: float) -> None:
            # Map this stage's 0-100% onto [pct_base, pct_base+pct_span] of the bar.
            scaled = pct_base + max(0.0, min(100.0, raw_pct)) * pct_span / 100.0
            p = max(0, min(99, int(scaled)))
            self._set(job_id, percent=p)
            self._hub.set_transcode_progress(p / 100.0)  # LEDs + display
            self._push_queue_state()  # keep the queue count/ETA live on the panel

        with self._lock:
            job_started = (self._jobs.get(job_id) or {}).get("started")

        def _track(output_pos: Optional[float]) -> None:
            # Live seconds-per-frame (single-stage only): the job's wall time so far
            # over the output frames produced. Marked stable once enough output
            # exists that the startup transient is amortized, so a canceled job that
            # ran long enough can still train the estimate model.
            if not (track_perf and src_fps and src_fps > 0 and job_started
                    and output_pos and output_pos > 0):
                return
            elapsed = time.monotonic() - job_started
            if elapsed <= 0:
                return
            self._set(job_id, perf_spf=elapsed / (output_pos * src_fps),
                      perf_stable=output_pos >= PERF_STABLE_MIN_OUTPUT_SEC)

        try:
            assert proc.stdout is not None
            t0 = time.monotonic()  # encode start, for the GStreamer speed estimate
            for raw in proc.stdout:
                now = time.monotonic()
                last_line[0] = now
                line = raw.strip()
                if is_gst:
                    pct = gst_progress_percent(line)
                    if pct is not None:
                        _emit(pct)
                    pos = gst_progress_position(line)
                    if pos is not None:
                        _track(pos)
                        elapsed = now - t0
                        if elapsed > 0.5:  # let the first second settle
                            speed = pos / elapsed
                            self._set(job_id, speed=f"{speed:.2f}x")
                            if src_fps and src_fps > 0:
                                fps = speed * src_fps
                                self._set(job_id, fps=round(fps, 1))
                                self._state.set_transcode_meta(fps=fps)
                    continue
                secs = progress_seconds(line)
                if secs is not None and duration and duration > 0:
                    _track(secs)
                    _emit(secs / duration * 100)
                elif line.startswith("fps="):
                    try:
                        fps = float(line.split("=", 1)[1])
                    except ValueError:
                        fps = 0.0
                    if fps > 0:
                        self._set(job_id, fps=fps)
                        self._state.set_transcode_meta(fps=fps)
                elif line.startswith("speed="):
                    speed = line.split("=", 1)[1].strip()
                    if speed and speed.upper() != "N/A":
                        self._set(job_id, speed=speed)
            proc.wait()
        finally:
            wd_stop.set()
            if watchdog is not None:
                watchdog.join(timeout=1.0)
            with self._lock:
                self._current_proc = None
                self._current_id = None

        if self._is_canceled(job_id):
            raise _Canceled("canceled")
        if proc.returncode != 0:
            raise TranscodeError(f"{engine} exited with code {proc.returncode}")

    def _is_canceled(self, job_id: int) -> bool:
        with self._lock:
            return self._jobs.get(job_id, {}).get("status") == "canceled"

    def _fail(self, job_id: int, exc: Exception, crash: bool = False) -> None:
        # A cancel that raced with a failure keeps the canceled status.
        if self._is_canceled(job_id):
            return
        self._set(job_id, status="error", error=str(exc))
        if crash:
            _LOG.exception("Transcode #%d crashed: %s", job_id, exc)
        else:
            _LOG.warning("Transcode #%d failed: %s", job_id, exc)

    def _trim_history(self) -> None:
        # Drop the oldest *finished* jobs beyond MAX_HISTORY (keep active ones).
        if len(self._order) <= MAX_HISTORY:
            return
        keep: list[int] = []
        removable = [
            i for i in self._order
            if self._jobs[i]["status"] in ("done", "error", "canceled")
        ]
        drop = set(removable[: max(0, len(self._order) - MAX_HISTORY)])
        for i in self._order:
            if i in drop:
                self._jobs.pop(i, None)
            else:
                keep.append(i)
        self._order = keep

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            proc = self._current_proc
        if proc is not None:
            proc.terminate()


def _encoder_label(enc: Encoder) -> str:
    """Human-facing encoder name, noting a hardware-decode offload if used.

    The offload keeps the same CPU *encoder* (so ``enc.name`` is unchanged and it
    is not a hardware encode), but the source is hardware-decoded -- worth showing
    in the job row / logs, e.g. ``cpu (hevc hw-decode)``.
    """
    return f"{enc.name} (hevc hw-decode)" if enc.decode_hwaccel else enc.name


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover - best effort
        pass
