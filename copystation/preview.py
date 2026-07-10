"""In-browser preview classification.

Clicking a file in the web browser plays it **directly** via the byte-range file
stream. Most browsers cannot decode 4K/HEVC smoothly (and the Cubie's VPU cannot
transcode it in real time either -- H.264 4K decode is rated 30fps on the A733),
so for such sources the player shows a hint that a transcode is needed for smooth
playback; the user starts one from the file (gear) dialog when they want it.

This module only *classifies* a source -- ``direct`` (plays as-is) vs
``transcode`` (plays but stutters, transcode for smooth) -- from an ffprobe. The
actual playback is the plain file stream in the web layer; there is no live
transcode here.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from .transcode import probe_video_info

_LOG = logging.getLogger("copystation.preview")


class PreviewError(Exception):
    """Base class for preview failures (mapped to HTTP codes by the web layer)."""


class PreviewUnavailable(PreviewError):
    """ffprobe is not installed / previews are off."""


def preview_mode(info: dict, max_direct_height: int = 1080) -> str:
    """``"direct"`` if the source plays in a browser as-is, else ``"transcode"``.

    Direct playback is fine for H.264 at or below ``max_direct_height`` in a
    browser-friendly container; anything else (4K, HEVC, mkv/avi, ...) plays but
    stutters, so the UI hints that a transcode gives smooth playback.
    """
    vcodec = str(info.get("vcodec") or "").lower()
    height = int(info.get("height") or 0)
    container = str(info.get("container") or "").lower()
    direct = (
        vcodec in ("h264", "avc1")
        and 0 < height <= int(max_direct_height)
        and container in ("mp4", "mov", "m4v", "webm")
    )
    return "direct" if direct else "transcode"


class PreviewManager:
    """Classifies browsable sources as direct-playable or transcode-for-smooth."""

    def __init__(self, config: Any, browse: Any, state: Any = None) -> None:
        if browse is None:
            raise PreviewError("preview requires the file browser (mounts)")
        self._browse = browse
        pv = (config.get("preview", {}) if config else {}) or {}
        self._max_direct_height = int(pv.get("max_direct_height", 1080))
        self._available = shutil.which("ffprobe") is not None
        self._probe_cache: dict[str, Any] = {}
        if not self._available:
            _LOG.warning("Preview enabled but ffprobe not found on PATH.")

    @property
    def available(self) -> bool:
        return self._available

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
        """Source properties + whether it needs transcoding for smooth playback."""
        if not self._available:
            raise PreviewUnavailable("ffprobe is not installed on the station")
        src = self._browse.resolve_file(device, path)  # -> BrowseError
        info = dict(self._probe(src))
        return {
            "mode": preview_mode(info, self._max_direct_height),
            "vcodec": info.get("vcodec"),
            "width": info.get("width"),
            "height": info.get("height"),
            "fps": info.get("fps"),
            "duration": info.get("duration"),
            "has_audio": info.get("has_audio"),
            "acodec": info.get("acodec"),
        }
