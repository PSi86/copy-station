"""What counts as a video, and which files at a volume's root form recordings.

Shared by the device watcher (sources that record to the root of their storage,
like the Walksnail air unit) and the transcode manager, so both work from one
definition of "video".
"""

from __future__ import annotations

from pathlib import Path

# Extensions treated as video. Used to pick the transcodable files of a folder
# and to anchor the recordings of a root-media source. Non-video files (photos,
# sidecars) are skipped by transcoding; for a root-media source they travel
# with the video they are named after (see :func:`recording_files`).
VIDEO_EXTS = frozenset({
    "mp4", "mov", "m4v", "mkv", "webm", "avi", "mts", "m2ts", "ts",
    "mpg", "mpeg", "wmv", "flv", "3gp", "3g2", "mxf", "insv",
})
# Deliberately NOT here: ``.lrv`` (DJI/Insta360 low-resolution preview proxies) --
# they are not worth transcoding and share a stem with the real clip, so they
# would only collide on the output name (e.g. DJI_0001.LRV vs DJI_0001.MP4).


def is_video_file(name: str) -> bool:
    """Whether ``name`` has a known video extension."""
    return Path(str(name)).suffix.lower().lstrip(".") in VIDEO_EXTS


def recording_files(root: Path) -> list[str]:
    """The files at the top of ``root`` that make up recordings.

    A recording is anchored on its video: every video file directly in ``root``
    (``VID0001.mp4``) plus every other file there whose name is the video's name
    up to its extension followed by a dot (``VID0001.osd``, ``VID0001.srt``, any
    extension). Sidecar types therefore never have to be listed -- a new one
    travels along without a code change. The dot keeps ``VID0001`` from picking
    up ``VID00010``'s files; names compare case-insensitively.

    Everything that belongs to no video is left out: the device's own files
    (``Avatar_version.txt``), folders, hidden files (``._VID0001.mp4`` is macOS
    metadata, not a second video) and sidecars whose video is gone. What this
    returns is what gets copied, verified and then deleted from the source, so
    anything not listed is never touched. Sorted, names relative to ``root``.
    """
    root = Path(root)
    try:
        names = [
            entry.name for entry in root.iterdir()
            if not entry.name.startswith(".") and entry.is_file() and not entry.is_symlink()
        ]
    except OSError:  # pragma: no cover - defensive (volume vanished)
        return []
    stems = {Path(name).stem.lower() for name in names if is_video_file(name)}
    if not stems:
        return []
    recordings = [
        name for name in names
        if any(name.lower().startswith(stem + ".") for stem in stems)
    ]
    return sorted(recordings, key=lambda name: (name.lower(), name))
