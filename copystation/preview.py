"""On-the-fly HLS preview: review a source video in the browser without
downloading it, transcoded live to a browser-friendly 1080p30 H.264 stream.

The web player asks for a **VOD** playlist that lists every segment up front, so
the timeline is fully seekable; each segment is transcoded **independently, on
demand** when the player requests it. A seek anywhere just fetches that segment
-- there is no long-running session to reposition. Segment transcodes are
serialised (one at a time: the Cubie has a single hardware encoder) and only run
while the station is idle (a copy/transcode owns the volumes otherwise).

Path selection:

* Sources that already play in a browser (H.264, <=1080p, mp4/mov/webm) are
  served **as-is** via the plain byte-range stream -- no transcode.
* Everything else (4K, HEVC, mkv/avi, ...) is transcoded per segment. On the
  Cubie the hardware encoder is used via **ffmpeg (fast input-seek + stream-copy)
  piped into a GStreamer OMX decode->scale->encode** pipeline -- the seek is done
  cheaply by ffmpeg so GStreamer only ever runs forward (no fragile time-seek).
  Other boards / the fallback use a pure ffmpeg segment transcode.

The playlist and the ffmpeg/GStreamer command building are pure and unit-tested;
the actual encode (and the hardware path in particular) is field-validated on the
device, like the rest of the transcode subsystem.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from .encoders import (
    _GST_DECODERS,
    available_encoders,
    available_gst_elements,
    build_ffmpeg_cmd,
    build_gstreamer_cmd,
    decoder_scale_factor,
    detect_board,
    gst_can_handle,
    select_encoders,
)
from .mounts import BrowseError
from .status import State
from .transcode import (
    gst_progress_percent,
    parse_bitrate,
    probe_duration,
    probe_video_info,
    progress_seconds,
)

# Cache-file key: 16 lowercase hex chars (a truncated SHA-1). Validates the path
# segment of the proxy-file endpoint (no traversal -- the server mints the key).
_PROXY_KEY_RE = re.compile(r"^[0-9a-f]{16}$")

_LOG = logging.getLogger("copystation.preview")


class PreviewError(Exception):
    """Base class for preview failures (mapped to HTTP codes by the web layer)."""


class PreviewUnavailable(PreviewError):
    """ffmpeg is not installed / previews are off."""


class PreviewBusy(PreviewError):
    """A copy or transcode is running, so the volumes are not free to read."""


# ----- pure helpers (unit-tested) ------------------------------------------- #


def segment_count(duration: float, seg_seconds: float) -> int:
    """Number of HLS segments covering ``duration`` at ``seg_seconds`` each."""
    if not duration or duration <= 0 or seg_seconds <= 0:
        return 0
    return int(math.ceil(duration / seg_seconds))


def build_playlist(duration: float, seg_seconds: float, query: str) -> str:
    """A seekable VOD ``.m3u8`` listing every segment (relative ``seg-<n>.ts``).

    ``query`` is the already-encoded ``device=..&path=..`` string appended to each
    segment URL so the (stateless) segment endpoint knows what to transcode.
    """
    n = segment_count(duration, seg_seconds)
    target = int(math.ceil(seg_seconds))
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for i in range(n):
        seg_dur = seg_seconds if i < n - 1 else max(0.001, duration - (n - 1) * seg_seconds)
        lines.append(f"#EXTINF:{seg_dur:.3f},")
        lines.append(f"seg-{i}.ts?{query}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def preview_mode(info: dict, max_direct_height: int = 1080) -> str:
    """``"direct"`` if the source plays in a browser as-is, else ``"hls"``.

    Direct playback (the plain range stream, no transcode) is used only for H.264
    at or below ``max_direct_height`` in a browser-friendly container; anything
    else (4K, HEVC, mkv/avi, ...) is transcoded to an HLS preview.
    """
    vcodec = str(info.get("vcodec") or "").lower()
    height = int(info.get("height") or 0)
    container = str(info.get("container") or "").lower()
    direct = (
        vcodec in ("h264", "avc1")
        and 0 < height <= int(max_direct_height)
        and container in ("mp4", "mov", "m4v", "webm")
    )
    return "direct" if direct else "hls"


def build_ffmpeg_segment_cmd(src: Any, start: float, seg_seconds: float,
                             height: int, fps: int, bitrate: Any) -> List[str]:
    """ffmpeg argv for one independent HLS segment (CPU), MPEG-TS on stdout.

    ``-ss`` **before** ``-i`` is a fast input seek to the keyframe at/before
    ``start``; ``-t`` bounds the segment. libx264 emits an IDR at the start, so the
    segment is self-contained (seekable). Downscaled to ``height`` and capped to
    ``fps`` (frame drop) so a 4K60 source becomes a light 1080p30 preview.
    """
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(src),
        "-t", f"{float(seg_seconds):.3f}",
        "-vf", f"scale=-2:{int(height)}", "-r", str(int(fps)),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-b:v", str(bitrate),
        "-c:a", "aac", "-ac", "2", "-b:a", "128k",
        "-f", "mpegts", "pipe:1",
    ]


def build_hw_segment_cmds(src: Any, info: dict, start: float, seg_seconds: float,
                          height: int, fps: int, bitrate: Any) -> Tuple[List[str], List[str]]:
    """(ffmpeg, gst-launch) argv pair for a hardware HLS segment on the Cubie.

    ffmpeg does the **seek** (fast, input-seek) and **stream-copies** the
    compressed video (plus AAC audio when present) as MPEG-TS to stdout; GStreamer
    reads it on stdin, hardware-decodes+downscales and hardware-encodes H.264,
    muxing MPEG-TS to stdout. So GStreamer only ever runs forward -- the seek never
    touches the (seek-averse) OMX pipeline.

    The downscale is done **in the decoder** via its ``scale`` property (0=full,
    1=1/2, 2=1/4) -- *no CPU element (videoscale/videorate) may sit between the OMX
    decoder and encoder*, or the shared VPU buffer pool fails to configure and the
    pipeline SIGSEGVs. So the produced height is the source divided by 1/2/4 to the
    nearest clean size not below ``height`` (exact height is not needed for a
    preview), and ``fps`` is **not** capped on this path (frame dropping would need
    that forbidden CPU element; the A733 decode is the bottleneck anyway).
    """
    aac_audio = bool(info.get("has_audio")) and str(info.get("acodec") or "").lower() == "aac"
    ff = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
          "-ss", f"{max(0.0, float(start)):.3f}", "-i", str(src),
          "-t", f"{float(seg_seconds):.3f}"]
    ff += (["-map", "0:v:0", "-map", "0:a:0?", "-c", "copy"] if aac_audio
           else ["-map", "0:v:0", "-c:v", "copy", "-an"])
    ff += ["-f", "mpegts", "pipe:1"]

    vcodec = str(info.get("vcodec") or "h264").lower()
    parse, decoder = _GST_DECODERS.get(vcodec, ("h264parse", "omxh264dec"))
    bps = parse_bitrate(bitrate) or 8_000_000
    scale = decoder_scale_factor(int(info.get("height") or 0), int(height))
    dec = [decoder] + ([] if scale == 0 else [f"scale={scale}"])
    gst = ["gst-launch-1.0", "-q", "fdsrc", "!", "tsdemux", "name=d",
           "d.", "!", "queue", "!", parse, "!", *dec, "!", "queue", "!",
           "omxh264videoenc", "control-rate=variable", f"target-bitrate={bps}", "!",
           "h264parse", "!", "mpegtsmux", "name=mux", "!", "fdsink"]
    if aac_audio:
        gst += ["d.", "!", "queue", "!", "aacparse", "!", "mux."]
    return ff, gst


# ----- manager -------------------------------------------------------------- #


class PreviewManager:
    """Serves per-segment on-demand HLS previews of browsable source videos."""

    def __init__(self, config: Any, browse: Any, state: Any) -> None:
        if browse is None:
            raise PreviewError("preview requires the file browser (mounts)")
        self._browse = browse
        self._state = state
        pv = (config.get("preview", {}) if config else {}) or {}
        self._height = int(pv.get("height", 1080))
        self._fps = int(pv.get("fps", 30))
        self._bitrate = pv.get("bitrate", "8M")
        self._seg_seconds = float(pv.get("segment_seconds", 4))
        self._max_direct_height = int(pv.get("max_direct_height", 1080))
        self._acceleration = str(pv.get("acceleration", "auto")).strip().lower()
        # Proxy previews: a downscaled H.264 file transcoded once, then played back
        # directly (smooth + natively seekable) -- the way to review 4K on a SoC
        # that can't decode 4K in real time for a live stream.
        self._proxy_height = int(pv.get("proxy_height", 540))
        self._proxy_bitrate = pv.get("proxy_bitrate", "6M")
        self._proxy_fallback_cpu = bool(pv.get("fallback_to_cpu", True))
        self._cache_dir = Path(pv.get("cache_dir", "/var/cache/copystation/preview"))
        self._cache_max = int(pv.get("cache_max_mb", 2048)) * 1024 * 1024
        self._available = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
        self._board = detect_board()
        self._encoders_avail = (
            available_encoders() | available_gst_elements() if self._available else set()
        )
        self._lock = threading.Lock()  # one preview transcode at a time (single VPU)
        self._probe_cache: dict[str, Any] = {}
        self._proxy_lock = threading.Lock()
        self._proxies: dict[str, dict] = {}  # key -> {state, percent, error, proc}
        if not self._available:
            _LOG.warning("Preview enabled but ffmpeg/ffprobe not found on PATH.")

    @property
    def available(self) -> bool:
        return self._available

    # ----- introspection --------------------------------------------------- #

    def _probe(self, src: Path) -> dict:
        key = str(src)
        try:
            st = src.stat()
            sig = (st.st_mtime, st.st_size)
        except OSError:
            sig = None
        cached = self._probe_cache.get(key)
        if cached is not None and cached[0] == sig:
            return cached[1]
        info = probe_video_info(src)
        self._probe_cache[key] = (sig, info)
        return info

    def info(self, device: str, path: str) -> dict:
        """Source properties + whether it needs transcoding (``mode``)."""
        if not self._available:
            raise PreviewUnavailable("ffmpeg is not installed on the station")
        src = self._browse.resolve_file(device, path)  # -> BrowseError (allow_download gate)
        info = dict(self._probe(src))
        mode = preview_mode(info, self._max_direct_height)
        return {
            "mode": mode,
            "vcodec": info.get("vcodec"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "duration": info.get("duration"),
            "has_audio": info.get("has_audio"),
            "acodec": info.get("acodec"),
        }

    def playlist(self, device: str, path: str) -> str:
        """The seekable VOD ``.m3u8`` for a source (raises if duration unknown)."""
        if not self._available:
            raise PreviewUnavailable("ffmpeg is not installed on the station")
        src = self._browse.resolve_file(device, path)
        duration = self._duration(src)
        if not duration or duration <= 0:
            raise PreviewError("cannot build a preview: source duration is unknown")
        query = urlencode({"device": device, "path": path})
        return build_playlist(duration, self._seg_seconds, query)

    def _duration(self, src: Path) -> Optional[float]:
        d = self._probe(src).get("duration")
        return d if d else probe_duration(src)

    # ----- segment transcode ----------------------------------------------- #

    def _use_hardware(self, info: dict) -> bool:
        if self._acceleration in ("cpu", "software", "none"):
            return False
        # H.264 sources only on the hardware preview path: ``omxhevcvideodec`` does
        # not propagate a framerate, so the H.264 encoder's (time-based) rate control
        # fails and it emits a ~100x oversized segment. An HEVC source therefore
        # falls back to the (correct, if slow) CPU ffmpeg path.
        return (
            self._board == "cubie"
            and "omxh264videoenc" in self._encoders_avail
            and str(info.get("vcodec") or "").lower() in ("h264", "avc1")
            and gst_can_handle(info)
        )

    def _spawn(self, src: Path, info: dict, start: float) -> List[subprocess.Popen]:
        if self._use_hardware(info):
            ff_cmd, gst_cmd = build_hw_segment_cmds(
                src, info, start, self._seg_seconds, self._height, self._fps, self._bitrate)
            ff = subprocess.Popen(ff_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            gst = subprocess.Popen(gst_cmd, stdin=ff.stdout,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            if ff.stdout is not None:
                ff.stdout.close()  # let ff receive SIGPIPE if gst goes away
            return [ff, gst]
        cmd = build_ffmpeg_segment_cmd(
            src, start, self._seg_seconds, self._height, self._fps, self._bitrate)
        return [subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)]

    def iter_segment(self, device: str, path: str, n: int) -> Iterator[bytes]:
        """Validate the request, then return a generator of the segment's bytes.

        The pre-flight checks (availability, busy, path resolution) run **eagerly**
        here -- not inside the generator -- so the web layer can map them to a
        status code before the streaming response starts (a generator body would
        otherwise only run mid-stream, after headers are sent). Segment transcodes
        are serialised (single hardware encoder) and refused while the station is
        busy; the transcoder is always torn down when the generator closes (client
        disconnect / seek away).
        """
        if not self._available:
            raise PreviewUnavailable("ffmpeg is not installed on the station")
        if self._state is not None and self._state.phase in (State.COPYING, State.TRANSCODING):
            raise PreviewBusy("the station is busy -- try again once it is idle")
        src = self._browse.resolve_file(device, path)  # -> BrowseError, eagerly
        info = self._probe(src)
        start = max(0, int(n)) * self._seg_seconds

        def _stream() -> Iterator[bytes]:
            with self._lock:
                procs = self._spawn(src, info, start)
                reader = procs[-1].stdout
                try:
                    assert reader is not None
                    while True:
                        chunk = reader.read(65536)
                        if not chunk:
                            break
                        yield chunk
                finally:
                    for proc in procs:
                        _terminate(proc)

        return _stream()

    # ----- proxy preview (transcode once, then play the file) --------------- #

    def _proxy_key(self, device: str, path: str, src: Path) -> str:
        try:
            st = src.stat()
            sig = f"{int(st.st_mtime)}:{st.st_size}"
        except OSError:
            sig = "0:0"
        raw = f"{device}|{path}|{sig}|{self._proxy_height}"
        return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]

    def _proxy_dst(self, key: str) -> Path:
        return self._cache_dir / f"{key}.mp4"

    def _set_proxy(self, key: str, **fields: Any) -> None:
        with self._proxy_lock:
            job = self._proxies.get(key)
            if job is not None:
                job.update(fields)

    def proxy_status(self, device: str, path: str) -> dict:
        """State of a source's proxy: ``ready`` (with ``url``), ``transcoding``
        (with ``percent``) or ``error``. Starts the transcode on first call.

        The proxy is a one-time downscaled H.264 file kept in a local cache; once
        ready it is played back directly (smooth, natively seekable) -- unlike the
        live stream, which the A733 cannot decode fast enough for 4K.
        """
        if not self._available:
            raise PreviewUnavailable("ffmpeg is not installed on the station")
        src = self._browse.resolve_file(device, path)  # -> BrowseError
        key = self._proxy_key(device, path, src)
        dst = self._proxy_dst(key)
        with self._proxy_lock:
            job = self._proxies.get(key)
            ready = dst.exists() and dst.stat().st_size > 0 and (job is None or job.get("state") == "ready")
            if ready:
                return {"state": "ready", "percent": 100, "url": f"/api/files/preview-proxy/{key}.mp4"}
            if job is None or job.get("state") == "error":
                # (Re)start: refuse if a copy/transcode already holds the volumes.
                if self._state is not None and self._state.phase in (State.COPYING, State.TRANSCODING):
                    raise PreviewBusy("the station is busy -- try again once it is idle")
                job = {"state": "transcoding", "percent": 0, "error": None, "proc": None,
                       "poked": time.monotonic()}
                self._proxies[key] = job
                threading.Thread(target=self._run_proxy, name=f"copystation-proxy-{key}",
                                 args=(src, dst, key), daemon=True).start()
            else:
                job["poked"] = time.monotonic()
            state, percent, error = job.get("state"), job.get("percent", 0), job.get("error")
        resp: dict = {"state": state, "percent": percent}
        if state == "ready":
            resp["url"] = f"/api/files/preview-proxy/{key}.mp4"
        elif state == "error":
            resp["error"] = error
        return resp

    def proxy_file(self, key: str) -> Path:
        """Absolute path of a ready proxy file (validated key), for the web layer."""
        if not _PROXY_KEY_RE.match(key or ""):
            raise BrowseError(f"bad proxy key {key!r}")
        dst = self._proxy_dst(key)
        if not dst.is_file():
            from .mounts import NotFound
            raise NotFound(f"proxy {key} not found")
        return dst

    def cancel_proxy(self, device: str, path: str) -> bool:
        """Abort a running proxy transcode (called when the viewer closes)."""
        try:
            src = self._browse.resolve_file(device, path)
        except BrowseError:
            return False
        key = self._proxy_key(device, path, src)
        with self._proxy_lock:
            job = self._proxies.get(key)
            proc = job.get("proc") if job and job.get("state") == "transcoding" else None
            if job is not None and job.get("state") == "transcoding":
                job["state"] = "canceled"
        if proc is not None:
            _terminate(proc)
            return True
        return False

    def _run_proxy(self, src: Path, dst: Path, key: str) -> None:
        # Hold the operation lock so a proxy never collides with a copy or a
        # transcode on the single VPU (non-blocking: bail if the station got busy).
        lock = getattr(self._state, "operation_lock", None)
        if lock is not None and not lock.acquire(blocking=False):
            self._set_proxy(key, state="error", error="station busy")
            return
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            info = self._probe(src)
            duration = info.get("duration") or probe_duration(src)
            preset = {"id": "proxy", "height": self._proxy_height, "vcodec": "libx264",
                      "bitrate": self._proxy_bitrate, "crf": 23, "preset": "veryfast"}
            encoders = select_encoders(
                preset, board=self._board, available=self._encoders_avail,
                acceleration=self._acceleration, fallback_to_cpu=self._proxy_fallback_cpu)
            tmp = dst.with_suffix(".part.mp4")
            last_exc: Optional[Exception] = None
            for enc in encoders:
                if self._proxy_canceled(key):
                    break
                try:
                    if enc.is_gstreamer and gst_can_handle(info):
                        self._run_proxy_encode(
                            key, build_gstreamer_cmd(enc, preset, src, tmp, info), duration, gst=True)
                    else:
                        self._run_proxy_encode(
                            key, build_ffmpeg_cmd(enc, preset, src, tmp), duration, gst=False)
                    os.replace(tmp, dst)
                    self._set_proxy(key, state="ready", percent=100)
                    self._reap_cache()
                    return
                except Exception as exc:  # try the next encoder (CPU fallback)
                    last_exc = exc
                    _unlink(tmp)
                    if self._proxy_canceled(key):
                        break
            if self._proxy_canceled(key):
                _unlink(tmp)
                self._set_proxy(key, state="error", error="canceled")
            else:
                _LOG.warning("Preview proxy for %s failed: %s", src, last_exc)
                self._set_proxy(key, state="error", error=str(last_exc or "transcode failed"))
        finally:
            if lock is not None:
                lock.release()

    def _proxy_canceled(self, key: str) -> bool:
        with self._proxy_lock:
            return (self._proxies.get(key) or {}).get("state") == "canceled"

    def _run_proxy_encode(self, key: str, cmd: List[str], duration: Optional[float],
                          gst: bool) -> None:
        stderr = subprocess.STDOUT if gst else subprocess.DEVNULL
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr, text=True)
        self._set_proxy(key, proc=proc)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.strip()
                if gst:
                    pct = gst_progress_percent(line)
                    if pct is not None:
                        self._set_proxy(key, percent=max(0, min(99, int(pct))))
                else:
                    secs = progress_seconds(line)
                    if secs is not None and duration and duration > 0:
                        self._set_proxy(key, percent=max(0, min(99, int(secs / duration * 100))))
            proc.wait()
        finally:
            self._set_proxy(key, proc=None)
        if self._proxy_canceled(key):
            raise PreviewError("canceled")
        if proc.returncode != 0:
            raise PreviewError(f"transcode exited with code {proc.returncode}")

    def _reap_cache(self) -> None:
        """Keep the proxy cache under the byte budget (drop the oldest first)."""
        try:
            files = sorted(self._cache_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
        except OSError:  # pragma: no cover - cache dir vanished
            return
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= self._cache_max:
                break
            try:
                sz = p.stat().st_size
                p.unlink()
                total -= sz
            except OSError:  # pragma: no cover - best effort
                pass


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover - best effort
        pass


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            proc.kill()
