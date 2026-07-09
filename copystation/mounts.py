"""Read-only mounting and safe path resolution for the web file browser.

The daemon mounts candidate volumes transiently under ``mount_base`` to probe
and copy them (see :mod:`copystation.devices`). The web file browser needs
*stable* access to the same volumes while a user is browsing, so it keeps its
own mounts under a **separate** base (``web.files.browse_base``), **read-only**:

* A read-only mount is safe next to the daemon's read-write mount of the same
  block device -- the browser never writes, so it cannot corrupt the filesystem;
  at worst a listing is briefly stale.
* Mounts are ref-counted per volume and reaped after an idle timeout, so a
  finished browse session releases the device on its own.

Only volumes returned by :func:`copystation.volumes.list_usb_volumes` can be
mounted -- that list excludes the OS/root device, so the browser can never reach
the OS partitions. The client passes an opaque ``sys_name`` (e.g. ``sdb1``); it
never supplies a device node or an absolute host path.

:func:`safe_resolve` (pure, unit-testable) is the path-traversal guard: it
resolves a client-supplied relative path under a mount root via ``realpath`` and
refuses anything that escapes the root (including via a symlink).
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import volumes

_LOG = logging.getLogger("copystation.mounts")


class BrowseError(Exception):
    """Base class for browse failures (mapped to HTTP codes by the web layer)."""


class UnknownVolume(BrowseError):
    """The requested ``sys_name`` is not a currently attached USB volume."""


class PathEscapesVolume(BrowseError):
    """The resolved path would leave the volume root (traversal/symlink)."""


class NotFound(BrowseError):
    """The requested path does not exist (or is the wrong type)."""


class MountFailed(BrowseError):
    """The volume could not be mounted read-only."""


def safe_resolve(root: Path, rel: str) -> Path:
    """Resolve ``rel`` under ``root`` and guarantee it stays inside ``root``.

    ``rel`` is treated as a path relative to ``root``. Leading separators, ``.``
    and empty segments are dropped; ``..`` and symlinks are neutralised by
    resolving with ``realpath`` and then checking containment. Raises
    :class:`PathEscapesVolume` on any escape. The root itself is allowed (an
    empty ``rel`` lists the volume root).
    """
    root_real = Path(os.path.realpath(root))
    rel = (rel or "").replace("\\", "/")
    candidate = root_real
    for part in rel.split("/"):
        if part in ("", ".", ".."):
            # Build only from plain names; '..' is handled by the realpath+
            # containment check below, never by trusting the input.
            if part == "..":
                candidate = candidate / part
            continue
        candidate = candidate / part
    candidate = Path(os.path.realpath(candidate))
    if candidate != root_real and root_real not in candidate.parents:
        raise PathEscapesVolume(f"path {rel!r} escapes the volume root")
    return candidate


class BrowseManager:
    """Ref-counted, read-only mounts of attached USB volumes for the web UI."""

    def __init__(self, config: Any) -> None:
        self._config = config
        files_cfg = (config.get("web", {}) or {}).get("files", {}) or {}
        self._base = Path(files_cfg.get("browse_base", "/run/copystation/browse"))
        # Read-write mounts (transcode output) live under a sibling base so they
        # never collide with the read-only browse mountpoints of the same device.
        self._rw_base = self._base.with_name(self._base.name + "-rw")
        self._idle = float(files_cfg.get("idle_unmount_seconds", 120))
        self._allow_download = bool(files_cfg.get("allow_download", True))
        self._lock = threading.RLock()
        # sys_name -> {"path": Path, "last": monotonic}
        self._mounts: dict[str, dict[str, Any]] = {}
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def allow_download(self) -> bool:
        return self._allow_download

    # ----- volume discovery -----------------------------------------------------

    def list_volumes(self) -> list[dict]:
        """All attached USB volumes (OS excluded). Never raises to the caller."""
        try:
            return volumes.list_usb_volumes(self._config)
        except Exception as exc:  # pragma: no cover - pyudev missing / udev error
            _LOG.warning("Volume enumeration failed: %s", exc)
            return []

    def _node_for(self, sys_name: str) -> str:
        """Device node for an attached USB volume, or raise :class:`UnknownVolume`.

        Looking the name up in the *live* volume list is the security gate: only
        currently attached, non-OS USB volumes are mountable.
        """
        for vol in self.list_volumes():
            if vol["sys_name"] == sys_name:
                return vol["device_node"]
        raise UnknownVolume(f"{sys_name!r} is not an attached USB volume")

    # ----- mounting -------------------------------------------------------------

    def _ensure_mounted(self, sys_name: str) -> Path:
        """Mount the volume read-only (if needed) and return its mountpoint.

        Overridable seam for tests: the pure listing/resolve logic all funnels
        through here, so a test can point a fake ``sys_name`` at a temp folder.
        """
        with self._lock:
            entry = self._mounts.get(sys_name)
            if entry is not None:
                entry["last"] = time.monotonic()
                return entry["path"]
            node = self._node_for(sys_name)
            mountpoint = self._base / sys_name
            self._do_mount(node, mountpoint)
            self._mounts[sys_name] = {"path": mountpoint, "last": time.monotonic()}
            self._ensure_reaper()
            _LOG.info("Browsing %s mounted read-only at %s", node, mountpoint)
            return mountpoint

    def mount_ro(self, sys_name: str) -> Path:
        """Public: ensure the volume is mounted read-only; return its mountpoint."""
        return self._ensure_mounted(sys_name)

    def mount_rw(self, sys_name: str) -> Path:
        """Mount the volume read-write under the rw base (transcode output).

        Not ref-counted/reaped -- the transcode worker mounts it for one job and
        unmounts it in a ``finally``. Safe because the shared ``operation_lock``
        guarantees no copy (and no daemon probe mount) touches the same device
        while a transcode holds it.
        """
        node = self._node_for(sys_name)
        mountpoint = self._rw_base / sys_name
        mountpoint.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["mount", "-o", "rw,nosuid,nodev,noexec", node, str(mountpoint)],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise MountFailed(f"cannot mount {node} read-write: {exc}") from exc
        _LOG.info("Transcode output %s mounted read-write at %s", node, mountpoint)
        return mountpoint

    def umount_rw(self, sys_name: str) -> None:
        self._do_umount(self._rw_base / sys_name)

    def _do_mount(self, node: str, mountpoint: Path) -> None:
        """Real read-only mount (Linux/root). Isolated so tests can bypass it."""
        mountpoint.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["mount", "-o", "ro,nosuid,nodev,noexec", node, str(mountpoint)],
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise MountFailed(f"cannot mount {node} read-only: {exc}") from exc

    def _do_umount(self, mountpoint: Path) -> None:
        subprocess.run(["umount", str(mountpoint)], capture_output=True, check=False)

    # ----- listing / download ---------------------------------------------------

    def list_dir(self, sys_name: str, rel: str) -> dict:
        """Directory listing of ``rel`` on the volume ``sys_name``."""
        root = self._ensure_mounted(sys_name)
        target = safe_resolve(root, rel)
        if not target.exists():
            raise NotFound(f"{rel!r} does not exist")
        if not target.is_dir():
            raise NotFound(f"{rel!r} is not a directory")
        entries: list[dict] = []
        for child in _iter_dir(target):
            try:
                is_dir = child.is_dir()
                stat = child.stat()
                entries.append(
                    {
                        "name": child.name,
                        "is_dir": is_dir,
                        "size": None if is_dir else stat.st_size,
                        "mtime": stat.st_mtime,
                    }
                )
            except OSError:  # pragma: no cover - entry vanished mid-listing
                continue
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        rel_norm = _rel_to_root(root, target)
        return {"device": sys_name, "path": rel_norm, "entries": entries}

    def resolve_input(self, sys_name: str, rel: str) -> Path:
        """Absolute path of an existing file on a volume, path-traversal-checked.

        Read-only access used both for downloads and as the transcode input.
        """
        root = self._ensure_mounted(sys_name)
        target = safe_resolve(root, rel)
        if not target.exists():
            raise NotFound(f"{rel!r} does not exist")
        if not target.is_file():
            raise NotFound(f"{rel!r} is not a file")
        return target

    def resolve_file(self, sys_name: str, rel: str) -> Path:
        """Absolute path of a downloadable file (subject to ``allow_download``)."""
        if not self._allow_download:
            raise PathEscapesVolume("downloads are disabled")
        return self.resolve_input(sys_name, rel)

    # ----- idle reaper / shutdown ----------------------------------------------

    def _ensure_reaper(self) -> None:
        if self._reaper is not None:
            return
        self._reaper = threading.Thread(
            target=self._reap_loop, name="copystation-browse-reaper", daemon=True
        )
        self._reaper.start()

    def _reap_loop(self) -> None:
        while not self._stop.wait(min(self._idle, 30.0) if self._idle else 30.0):
            self._reap_idle()

    def _reap_idle(self) -> None:
        if self._idle <= 0:
            return
        now = time.monotonic()
        with self._lock:
            stale = [
                name for name, e in self._mounts.items()
                if now - e["last"] >= self._idle
            ]
            for name in stale:
                entry = self._mounts.pop(name)
                self._do_umount(entry["path"])
                _LOG.info("Browsing %s unmounted (idle)", name)

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for name, entry in list(self._mounts.items()):
                self._do_umount(entry["path"])
                self._mounts.pop(name, None)


def _iter_dir(path: Path):
    """Directory children as Paths (isolated for a stable, testable listing)."""
    return list(path.iterdir())


def _rel_to_root(root: Path, target: Path) -> str:
    """POSIX-style path of ``target`` relative to ``root`` ("" for the root)."""
    try:
        rel = target.relative_to(root)
    except ValueError:  # pragma: no cover - safe_resolve already guarantees this
        return ""
    return "" if str(rel) == "." else rel.as_posix()
