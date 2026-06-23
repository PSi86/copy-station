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
import threading
import time
from pathlib import Path
from typing import Callable, Optional

# Callback invoked with the number of bytes copied so far.
ProgressCallback = Callable[[int], None]

# Callback returning True when the copy should be aborted (e.g. the source
# device was unplugged). Polled during the copy so we don't wait for the USB/SCSI
# I/O timeout (~7-10 s) before reacting.
AbortCheck = Callable[[], bool]

# How often the abort watcher polls while a copy is running.
_ABORT_POLL_SECONDS = 0.5


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


def copy_tree(
    src: Path,
    dst: Path,
    on_progress: Optional[ProgressCallback] = None,
    abort_check: Optional[AbortCheck] = None,
) -> None:
    """Copy the contents of ``src`` into ``dst`` (target folder is created).

    Uses ``rsync`` if available, otherwise ``shutil``. ``src`` must exist. If
    ``on_progress`` is given it is called with the cumulative byte count as the
    copy proceeds. If ``abort_check`` is given it is polled during the copy and,
    when it returns True, the copy is stopped promptly with ``SourceVanishedError``.
    """
    src = Path(src)
    dst = Path(dst)
    if not src.is_dir():
        raise SourceVanishedError(f"Source no longer present: {src}")

    dst.mkdir(parents=True, exist_ok=True)

    if _rsync_available():
        _copy_with_rsync(src, dst, on_progress, abort_check)
    else:
        _copy_with_shutil(src, dst, on_progress, abort_check)


_SOURCE_DISCONNECTED_MSG = (
    "Source device was disconnected during the copy. Nothing was deleted -- "
    "reconnect it and start again."
)


def _describe_rsync_failure(code: int, stderr: str) -> TransferError:
    """Translate an rsync exit code / stderr into a user-friendly error."""
    low = stderr.lower()
    if "input/output error" in low or "read errors" in low:
        return SourceVanishedError(_SOURCE_DISCONNECTED_MSG)
    if "no space left" in low:
        return InsufficientSpaceError(
            "The target ran out of space during the copy. Nothing was deleted."
        )
    if code == 24:
        return SourceVanishedError(
            "Some source files disappeared during the copy. Nothing was deleted."
        )
    if code == 23:
        return TransferError(
            "Some files could not be copied (partial transfer). Nothing was "
            "deleted -- check the source and target and try again."
        )
    detail = f" ({stderr.splitlines()[0]})" if stderr else ""
    return TransferError(
        f"Copy failed (rsync code {code}).{detail} Nothing was deleted."
    )


def _copy_with_rsync(
    src: Path,
    dst: Path,
    on_progress: Optional[ProgressCallback],
    abort_check: Optional[AbortCheck] = None,
) -> None:
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

    # Watch for a disconnected source in the background and kill rsync at once,
    # instead of waiting for its I/O timeout. The reading loop below unblocks as
    # soon as the killed process closes its stdout.
    aborted = threading.Event()
    watcher = _start_abort_watcher(proc, abort_check, aborted)

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
    if watcher is not None:
        watcher.join(timeout=1.0)
    stderr = proc.stderr.read().decode("utf-8", "ignore").strip() if proc.stderr else ""

    if aborted.is_set():
        raise SourceVanishedError(_SOURCE_DISCONNECTED_MSG)
    if proc.returncode != 0:
        raise _describe_rsync_failure(proc.returncode, stderr)


def _start_abort_watcher(
    proc: "subprocess.Popen", abort_check: Optional[AbortCheck], aborted: threading.Event
) -> Optional[threading.Thread]:
    """Poll ``abort_check`` while ``proc`` runs; terminate it on abort."""
    if abort_check is None:
        return None

    def _watch() -> None:
        while proc.poll() is None:
            try:
                stop = abort_check()
            except Exception:  # pragma: no cover - defensive
                stop = False
            if stop:
                aborted.set()
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                    proc.kill()
                return
            time.sleep(_ABORT_POLL_SECONDS)

    thread = threading.Thread(target=_watch, daemon=True)
    thread.start()
    return thread


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _copy_with_shutil(
    src: Path,
    dst: Path,
    on_progress: Optional[ProgressCallback],
    abort_check: Optional[AbortCheck] = None,
) -> None:
    done = 0
    for path in sorted(src.rglob("*")):
        if abort_check is not None and abort_check():
            raise SourceVanishedError(_SOURCE_DISCONNECTED_MSG)
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
