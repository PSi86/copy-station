"""Copy, verification and cleanup of the source.

The actual transfer is done preferably via ``rsync`` (proven, resumable). If
``rsync`` is not available -- e.g. on the Windows dev machine during
simulation/tests -- it transparently falls back to a pure Python copy
(``shutil``). Both paths produce the same result: the CONTENTS of the source
folder end up in the target folder.

Verification is intentionally kept fast: it compares file count and file sizes
(no checksums). Only on successful verification may the caller clear the source.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

# Callback invoked with the number of bytes copied so far.
ProgressCallback = Callable[[int], None]


class TransferError(Exception):
    """Base class for all transfer errors."""


class InsufficientSpaceError(TransferError):
    """The target does not have enough free space for the source."""


class VerificationError(TransferError):
    """Source and target do not match after the copy."""


class SourceVanishedError(TransferError):
    """The source disappeared during the transfer."""


def dir_signature(root: Path) -> dict[str, int]:
    """Snapshot of a directory tree as ``{relative_path: size}``.

    Basis for both the space calculation and the verification. Paths are
    normalised with ``/`` so the comparison is platform independent.
    """
    root = Path(root)
    signature: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            signature[rel] = path.stat().st_size
    return signature


def total_size(root: Path) -> int:
    """Total size of all files below ``root`` in bytes."""
    return sum(dir_signature(root).values())


def check_free_space(target_root: Path, required_bytes: int, margin: float = 1.02) -> None:
    """Make sure there is enough space on the target.

    ``margin`` reserves a small buffer (default 2 %) for filesystem overhead.
    Raises ``InsufficientSpaceError`` when space is too low.
    """
    free = shutil.disk_usage(target_root).free
    needed = int(required_bytes * margin)
    if free < needed:
        raise InsufficientSpaceError(
            f"Not enough space on the target: need ~{needed} bytes, "
            f"free {free} bytes"
        )


def _rsync_available() -> bool:
    return shutil.which("rsync") is not None


# Matches the leading transferred-byte count of an rsync `--info=progress2`
# line, e.g. "  1,234,567  45%  12.34MB/s    0:00:12".
_RSYNC_PROGRESS_RE = re.compile(r"^\s*([\d,]+)\s+\d+%")


def parse_rsync_progress(fragment: str) -> Optional[int]:
    """Parse the transferred-byte count from one rsync progress2 fragment.

    Returns the byte count, or ``None`` if the fragment is not a progress line.
    Pure function -- unit-testable without rsync.
    """
    match = _RSYNC_PROGRESS_RE.match(fragment)
    if not match:
        return None
    return int(match.group(1).replace(",", ""))


def copy_tree(src: Path, dst: Path, on_progress: Optional[ProgressCallback] = None) -> None:
    """Copy the contents of ``src`` into ``dst`` (target folder is created).

    Uses ``rsync`` if available, otherwise ``shutil``. ``src`` must exist. If
    ``on_progress`` is given it is called with the cumulative byte count as the
    copy proceeds.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_dir():
        raise SourceVanishedError(f"Source no longer present: {src}")

    dst.mkdir(parents=True, exist_ok=True)

    if _rsync_available():
        _copy_with_rsync(src, dst, on_progress)
    else:
        _copy_with_shutil(src, dst, on_progress)


def _copy_with_rsync(src: Path, dst: Path, on_progress: Optional[ProgressCallback]) -> None:
    # Trailing slash on the source => the CONTENTS of src end up in dst.
    src_arg = str(src).rstrip("/\\") + "/"
    cmd = [
        "rsync",
        "-a",
        "--no-perms",
        "--no-owner",
        "--no-group",
        "--info=progress2",
        src_arg,
        str(dst),
    ]
    # LC_ALL=C so the byte count uses commas as the thousands separator,
    # matching the parser above regardless of the system locale.
    env = {"LC_ALL": "C"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={**_os_environ(), **env},
    )
    assert proc.stdout is not None
    buffer = b""
    while True:
        chunk = proc.stdout.read(64)
        if not chunk:
            break
        buffer += chunk
        # progress2 separates updates with carriage returns, not newlines.
        fragments = re.split(rb"[\r\n]", buffer)
        buffer = fragments.pop()
        if on_progress:
            for fragment in fragments:
                done = parse_rsync_progress(fragment.decode("ascii", "ignore"))
                if done is not None:
                    on_progress(done)
    proc.wait()
    stderr = proc.stderr.read().decode("utf-8", "ignore").strip() if proc.stderr else ""
    if proc.returncode != 0:
        # rsync 24 = "some files vanished" -- the source was removed during the
        # copy; treat it as a specific error.
        if proc.returncode == 24:
            raise SourceVanishedError(stderr or "rsync: source vanished")
        raise TransferError(f"rsync failed (code {proc.returncode}): {stderr}")


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _copy_with_shutil(src: Path, dst: Path, on_progress: Optional[ProgressCallback]) -> None:
    done = 0
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            done += path.stat().st_size
            if on_progress:
                on_progress(done)


def verify(src: Path, dst: Path) -> None:
    """Fast verification: same files (relative path) and same sizes.

    Raises ``VerificationError`` with a descriptive message on any mismatch.
    """
    src_sig = dir_signature(src)
    dst_sig = dir_signature(dst)

    if src_sig == dst_sig:
        return

    missing = sorted(set(src_sig) - set(dst_sig))
    extra = sorted(set(dst_sig) - set(src_sig))
    size_mismatch = sorted(
        rel
        for rel in set(src_sig) & set(dst_sig)
        if src_sig[rel] != dst_sig[rel]
    )

    details = []
    if missing:
        details.append(f"missing in target: {len(missing)} ({missing[:5]})")
    if extra:
        details.append(f"unexpected in target: {len(extra)} ({extra[:5]})")
    if size_mismatch:
        details.append(f"size mismatch: {len(size_mismatch)} ({size_mismatch[:5]})")

    raise VerificationError("Verification failed -- " + "; ".join(details))


def cleanup_source(media_dir: Path, keep_folder: bool = True) -> None:
    """Delete the media in the source media folder -- never format.

    ``keep_folder=True``: the folder (e.g. DCIM) is kept, only its contents are
    removed. ``keep_folder=False``: the folder itself is deleted (the camera
    recreates it on next start). Other folders on the source are never touched.
    """
    media_dir = Path(media_dir)
    if not media_dir.is_dir():
        return

    if keep_folder:
        for entry in media_dir.iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    else:
        shutil.rmtree(media_dir)
