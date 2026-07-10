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
from pathlib import Path
from typing import Any, List, Optional

from .encoders import (
    Encoder,
    available_encoders,
    available_gst_elements,
    build_ffmpeg_cmd,
    build_gstreamer_cmd,
    cpu_encoder,
    default_bitrate,
    detect_board,
    gst_can_handle,
    gstreamer_output_height,
    select_encoders,
)
from .mounts import BrowseError, NotFound, safe_resolve
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

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class TranscodeError(Exception):
    """Base class for transcode errors (mapped to HTTP codes by the web layer)."""


class TranscodeUnavailable(TranscodeError):
    """ffmpeg is not installed / the feature is off."""


class UnknownPreset(TranscodeError):
    """The requested preset id is not configured."""


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
        "vcodec": None, "width": 0, "height": 0, "fps": 0.0,
        "has_audio": False, "acodec": None,
        "container": Path(str(src)).suffix.lower().lstrip("."),
    }
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate"
             ":stream_disposition=attached_pic",
             "-of", "json", str(src)],
            capture_output=True, text=True, check=True,
        ).stdout
        for st in json.loads(out).get("streams", []):
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
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError):
        pass
    return info


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


class TranscodeManager:
    """Single-worker transcode job queue backed by ffmpeg."""

    def __init__(self, config: Any, hub: Any, browse: Any) -> None:
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
        self._available = ffmpeg_available()
        # Detect the board and probe which encoders ffmpeg actually has, once at
        # startup, so hardware acceleration can be picked per job without re-probing.
        self._board = detect_board()
        # Probe both ffmpeg encoders and GStreamer elements: the Cubie's hardware
        # H.264 encoder is a GStreamer OMX element, not an ffmpeg encoder.
        self._encoders_avail = (
            available_encoders() | available_gst_elements() if self._available else set()
        )
        self._queue: "queue.Queue[int]" = queue.Queue()
        self._lock = threading.Lock()
        self._jobs: dict[int, dict] = {}
        self._order: list[int] = []
        self._seq = 0
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

    def snapshot(self) -> dict:
        now = time.monotonic()
        with self._lock:
            jobs = [self._public_job(self._jobs[i], now) for i in reversed(self._order)]
        return {
            "available": self._available,
            "output_dirname": self._output_dirname,
            "board": self._board,
            "acceleration": self._acceleration,
            "presets": [
                {"id": p.get("id"), "label": p.get("label", p.get("id"))}
                for p in self._presets
            ],
            "jobs": jobs,
        }

    @staticmethod
    def _public_job(job: dict, now: float) -> dict:
        """Copy of a job for the API, with live elapsed/ETA for a running one."""
        j = dict(job)
        started = j.pop("started", None)
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

    def submit(
        self,
        input_device: str,
        input_path: str,
        preset_id: str,
        output_device: Optional[str] = None,
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
        out_dev = output_device or input_device
        with self._lock:
            self._seq += 1
            job_id = self._seq
            job = {
                "id": job_id,
                "status": "queued",
                "input_device": input_device,
                "input_path": input_path,
                "output_device": out_dev,
                "preset": str(preset_id),
                "percent": 0,
                "filename": None,
                "output_path": None,
                "encoder": None,       # which encoder actually ran (cpu / h264_v4l2m2m ...)
                "hw": False,           # True if a hardware encoder was used
                "input_size": None,    # source file size in bytes
                "output_size": None,   # transcoded file size in bytes (once done)
                "ram_buffered": False, # True if staged through a RAM tmpfs
                "fps": None,           # live encode rate (frames/second)
                "speed": None,         # ffmpeg speed relative to realtime (e.g. "2.5x")
                "error": None,
                "started": None,       # wall clock (time.monotonic) when it starts running
            }
            self._jobs[job_id] = job
            self._order.append(job_id)
            self._trim_history()
            snap = dict(job)
        self._queue.put(job_id)
        self._ensure_worker()
        _LOG.info("Transcode queued (#%d): %s:%s -> %s [%s]",
                  job_id, input_device, input_path, out_dev, preset_id)
        return snap

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
            self._process(job_id)

    def _set(self, job_id: int, **fields: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None:
                job.update(fields)

    def _process(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] == "canceled":
                return
            input_device = job["input_device"]
            input_path = job["input_path"]
            output_device = job["output_device"]
            preset_id = job["preset"]

        # Serialise with the copy daemon (no concurrent mount/write of a volume)
        # AND take over every status indicator for the duration (phase TRANSCODING
        # overrides ready/detecting/copying on the LEDs, e-paper and web). The
        # previous phase is restored at the end so device detection resumes.
        with self._state.operation_lock:
            started = time.monotonic()
            self._set(job_id, status="running", started=started)
            out_name = output_name(Path(input_path).name, preset_id)
            out_rel = f"{self._output_dirname}/{out_name}"
            self._set(job_id, filename=out_name, output_path=out_rel)
            same_device = input_device == output_device
            prev_phase = self._state.phase
            if prev_phase is State.TRANSCODING:  # defensive: never restore to ourselves
                prev_phase = State.READY
            self._hub.begin_transcode(Path(input_path).name)
            try:
                # A block device can't be mounted read-only and read-write at once
                # (shared superblock -> the read-only one wins), so drop any browse
                # mount of the output device before mounting it read-write. Under
                # the operation lock the daemon holds no mount of it either.
                self._browse.release(output_device)
                out_root = self._browse.mount_rw(output_device)
                try:
                    # Read the input from the read-write mount when it is the same
                    # device; otherwise mount the (different) input device read-only.
                    in_root = out_root if same_device else self._browse.mount_ro(input_device)
                    src = safe_resolve(in_root, input_path)
                    if not src.is_file():
                        raise NotFound(f"{input_path!r} is not a file")
                    try:
                        input_size = src.stat().st_size
                    except OSError:
                        input_size = 0
                    self._set(job_id, input_size=input_size)
                    self._state.set_transcode_meta(input_size=input_size)
                    out_dir = out_root / self._output_dirname
                    out_dir.mkdir(parents=True, exist_ok=True)
                    dst = out_dir / out_name
                    self._encode(job_id, src, dst, self._preset(preset_id))
                    try:
                        self._set(job_id, output_size=dst.stat().st_size)
                    except OSError:  # pragma: no cover - defensive
                        pass
                    subprocess.run(["sync"], check=False)
                finally:
                    self._browse.umount_rw(output_device)
                    if not same_device:
                        self._browse.release(input_device)
                with self._lock:
                    job = self._jobs.get(job_id)
                    if job is not None and job["status"] == "running":
                        job["status"] = "done"
                        job["percent"] = 100
                _LOG.info("Transcode #%d done -> %s:%s", job_id, output_device, out_rel)
                self._hub.finish_transcode(prev_phase)
            except _Canceled:
                self._set(job_id, status="canceled")
                _LOG.info("Transcode #%d canceled", job_id)
                self._hub.finish_transcode(prev_phase)  # a cancel is not an error
            except (BrowseError, TranscodeError) as exc:
                self._end_failed(job_id, exc, prev_phase)
            except Exception as exc:  # pragma: no cover - defensive
                self._end_failed(job_id, exc, prev_phase, crash=True)

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
        info: Optional[dict] = None  # source media info, probed once if a GStreamer
        last_exc: Optional[TranscodeError] = None  # encoder is among the candidates
        for idx, enc in enumerate(encoders):
            if enc.is_gstreamer:
                if info is None:
                    info = probe_video_info(src)
                if not gst_can_handle(info):
                    _LOG.info(
                        "Transcode #%d: %s cannot handle this source "
                        "(codec=%s %sx%s container=%s audio=%s) -- skipping to next",
                        job_id, enc.name, info.get("vcodec"), info.get("width"),
                        info.get("height"), info.get("container"),
                        info.get("acodec") if info.get("has_audio") else "none",
                    )
                    continue
            self._set(job_id, encoder=enc.name, hw=enc.is_hardware, percent=0)
            # Record the encoder on the shared state too (shown on the display).
            self._hub.set_transcode_progress(0.0, enc.name, enc.is_hardware)
            _LOG.info("Transcode #%d: encoding with %s (%s, %s)",
                      job_id, enc.name, enc.kind, enc.engine)
            try:
                if enc.is_gstreamer:
                    self._run_gstreamer(job_id, src, dst, preset, info, enc, duration)
                else:
                    self._run_encode(job_id, build_ffmpeg_cmd(enc, preset, src, dst),
                                     duration, "ffmpeg")
                return
            except _Canceled:
                _cleanup(dst)
                raise
            except TranscodeError as exc:
                last_exc = exc
                _cleanup(dst)
                has_more = idx + 1 < len(encoders)
                if has_more:
                    nxt = encoders[idx + 1].name
                    _LOG.warning(
                        "Transcode #%d: encoder %s failed (%s) -- falling back to %s",
                        job_id, enc.name, exc, nxt,
                    )
                    self._set(job_id, note=f"{enc.name} failed, retrying with {nxt}")
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
        """
        src_fps = info.get("fps")
        target_h = int(preset.get("height", 0) or 0)
        out_h = gstreamer_output_height(int(info.get("height") or 0), target_h)
        if not (target_h > 0 and out_h > 0 and out_h != target_h):
            self._run_encode(job_id, build_gstreamer_cmd(enc, preset, src, dst, info),
                             duration, "gstreamer", src_fps=src_fps)
            return
        # Two-stage: hardware decode-scale to out_h, then a CPU scale to target_h.
        stage1 = dst.parent / f"{dst.stem}.stage1.mp4"
        _LOG.info("Transcode #%d: HW %dp, then CPU finish -> %dp", job_id, out_h, target_h)
        self._set(job_id, note=f"HW {out_h}p, CPU finish {target_h}p")
        try:
            self._run_encode(job_id, build_gstreamer_cmd(enc, preset, src, stage1, info),
                             duration, "gstreamer", src_fps=src_fps)
            self._set(job_id, encoder=f"{enc.name}+cpu", percent=0)
            self._run_encode(job_id, build_ffmpeg_cmd(
                cpu_encoder(str(preset.get("vcodec", "libx264"))), preset, stage1, dst),
                duration, "ffmpeg")
        finally:
            _cleanup(stage1)

    def _run_encode(self, job_id: int, cmd: List[str], duration: Optional[float],
                    engine: str = "ffmpeg", src_fps: Optional[float] = None) -> None:
        """Run one encoder subprocess, tracking progress and honouring cancel.

        ``engine`` selects how progress is read: ffmpeg's ``-progress`` stream, or
        a GStreamer ``progressreport``. ffmpeg reports its own fps/speed; GStreamer
        does not, so for the GStreamer path speed is derived from the output
        position over the elapsed wall time and fps from ``src_fps`` * speed. A
        GStreamer job also gets a stall watchdog (the OMX stack can wedge; see
        ``GST_STALL_SECONDS``). GStreamer's stderr is merged into stdout so the
        ``progressreport`` lines are captured regardless of the chatty OMX output.
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
                        p = max(0, min(99, int(pct)))
                        self._set(job_id, percent=p)
                        self._hub.set_transcode_progress(p / 100.0)  # LEDs + display
                    pos = gst_progress_position(line)
                    if pos is not None:
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
                    pct = max(0, min(99, int(secs / duration * 100)))
                    self._set(job_id, percent=pct)
                    self._hub.set_transcode_progress(pct / 100.0)  # drives LEDs + display
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

    def _end_failed(self, job_id: int, exc: Exception, prev_phase, crash: bool = False) -> None:
        """Mark the job failed and surface the error on every backend (ERROR phase).

        Unless it was actually canceled (a race), in which case the previous phase
        is simply restored -- a cancel is not an error.
        """
        self._fail(job_id, exc, crash=crash)
        if self._is_canceled(job_id):
            self._hub.finish_transcode(prev_phase)
        else:
            self._hub.fail_transcode(f"Transcode failed: {exc}")

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


def _cleanup(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:  # pragma: no cover - best effort
        pass
