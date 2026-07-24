"""In-browser preview classification.

Clicking a file in the web browser plays it **directly** via the byte-range file
stream. A frame clearly larger than Full HD is the heavy case that stutters in a
browser, so for such sources the player shows a hint that a transcode gives
smooth playback; the user starts one from the file (gear) dialog when they want
it. Up to and including Full HD plays fine, so no hint is shown there regardless
of codec or container (a 540p HEVC/mkv clip is not flagged).

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


# Coded-height padding tolerance: many cameras encode "1080p" as 1088 display
# lines (a coding-block multiple), which is still Full HD -- allow it so a 1080p
# clip never trips the hint.
_HEIGHT_TOLERANCE = 8


def preview_mode(info: dict, max_direct_height: int = 1080) -> str:
    """``"direct"`` if the source plays in a browser as-is, else ``"transcode"``.

    The hint is driven **only by resolution**: a frame clearly larger than Full HD
    (``max_direct_height``, plus a small padding tolerance) is heavy enough to
    stutter, so the UI suggests a transcode for smooth playback. Everything up to
    and including Full HD plays fine regardless of codec or container, so a 540p or
    1080p clip -- HEVC, mkv or otherwise -- never trips the hint. (A source the
    browser cannot decode at all is handled separately by the player's own error
    fallback, not here.)
    """
    height = int(info.get("height") or 0)
    return "transcode" if height > int(max_direct_height) + _HEIGHT_TOLERANCE else "direct"


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
