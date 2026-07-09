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

import logging
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

from .mounts import BrowseError

_LOG = logging.getLogger("copystation.transcode")

# Keep at most this many finished/failed jobs in the visible history.
MAX_HISTORY = 20

_SAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


class TranscodeError(Exception):
    """Base class for transcode errors (mapped to HTTP codes by the web layer)."""


class TranscodeUnavailable(TranscodeError):
    """ffmpeg is not installed / the feature is off."""


class UnknownPreset(TranscodeError):
    """The requested preset id is not configured."""


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


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


def build_ffmpeg_cmd(preset: dict, src: Any, dst: Any) -> List[str]:
    """ffmpeg argument list for one transcode (pure -- no execution).

    ``preset.height`` downscales to that height keeping the aspect ratio (width
    auto, forced even via ``scale=-2:H``); ``height`` of 0/absent keeps the
    source resolution. Progress is emitted on stdout via ``-progress pipe:1``.
    """
    height = int(preset.get("height", 0) or 0)
    vcodec = str(preset.get("vcodec", "libx264"))
    crf = preset.get("crf", 23)
    ff_preset = str(preset.get("preset", "medium"))

    cmd: List[str] = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(src)]
    if height > 0:
        cmd += ["-vf", f"scale=-2:{height}"]
    cmd += ["-c:v", vcodec, "-crf", str(crf), "-preset", ff_preset]
    cmd += ["-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    cmd += ["-progress", "pipe:1", "-nostats", str(dst)]
    return cmd


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


class TranscodeManager:
    """Single-worker transcode job queue backed by ffmpeg."""

    def __init__(self, config: Any, state: Any, browse: Any) -> None:
        if browse is None:
            raise TranscodeError("transcoding requires the file browser (mounts)")
        self._config = config
        self._state = state
        self._browse = browse
        tc = config.get("transcode", {}) or {}
        self._output_dirname = str(tc.get("output_dirname", "Transcoded"))
        self._presets = list(tc.get("presets", []))
        self._available = ffmpeg_available()
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

    # ----- introspection --------------------------------------------------------

    def _preset(self, preset_id: str) -> dict:
        for preset in self._presets:
            if str(preset.get("id")) == str(preset_id):
                return preset
        raise UnknownPreset(f"unknown preset {preset_id!r}")

    def snapshot(self) -> dict:
        with self._lock:
            jobs = [dict(self._jobs[i]) for i in reversed(self._order)]
        return {
            "available": self._available,
            "output_dirname": self._output_dirname,
            "presets": [
                {"id": p.get("id"), "label": p.get("label", p.get("id"))}
                for p in self._presets
            ],
            "jobs": jobs,
        }

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
                "error": None,
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

        # Serialise with the copy daemon: no concurrent mount/write of a volume.
        with self._state.operation_lock:
            self._set(job_id, status="running")
            out_name = output_name(Path(input_path).name, preset_id)
            out_rel = f"{self._output_dirname}/{out_name}"
            self._set(job_id, filename=out_name, output_path=out_rel)
            try:
                src = self._browse.resolve_input(input_device, input_path)
                out_root = self._browse.mount_rw(output_device)
                try:
                    out_dir = out_root / self._output_dirname
                    out_dir.mkdir(parents=True, exist_ok=True)
                    dst = out_dir / out_name
                    self._run_ffmpeg(job_id, src, dst, self._preset(preset_id))
                    subprocess.run(["sync"], check=False)
                finally:
                    self._browse.umount_rw(output_device)
            except BrowseError as exc:
                self._set(job_id, status="error", error=str(exc))
                _LOG.warning("Transcode #%d failed: %s", job_id, exc)
                return
            except TranscodeError as exc:
                self._set(job_id, status="error", error=str(exc))
                _LOG.warning("Transcode #%d failed: %s", job_id, exc)
                return
            except Exception as exc:  # pragma: no cover - defensive
                self._set(job_id, status="error", error=str(exc))
                _LOG.exception("Transcode #%d crashed: %s", job_id, exc)
                return

        with self._lock:
            job = self._jobs.get(job_id)
            if job is not None and job["status"] == "running":
                job["status"] = "done"
                job["percent"] = 100
        _LOG.info("Transcode #%d done -> %s:%s", job_id, output_device, out_rel)

    def _run_ffmpeg(self, job_id: int, src: Path, dst: Path, preset: dict) -> None:
        duration = probe_duration(src)
        cmd = build_ffmpeg_cmd(preset, src, dst)
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
            )
        except OSError as exc:
            raise TranscodeError(f"ffmpeg could not start: {exc}") from exc

        with self._lock:
            self._current_proc = proc
            self._current_id = job_id
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                secs = progress_seconds(line.strip())
                if secs is not None and duration and duration > 0:
                    pct = max(0, min(99, int(secs / duration * 100)))
                    self._set(job_id, percent=pct)
            proc.wait()
        finally:
            with self._lock:
                self._current_proc = None
                self._current_id = None

        with self._lock:
            canceled = self._jobs.get(job_id, {}).get("status") == "canceled"
        if canceled:
            _cleanup(dst)
            raise TranscodeError("canceled")
        if proc.returncode != 0:
            _cleanup(dst)
            raise TranscodeError(f"ffmpeg exited with code {proc.returncode}")

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
