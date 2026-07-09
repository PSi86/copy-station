"""Web file browser: path-traversal safety and the file endpoints.

Hardware-free: a fake BrowseManager points a fake volume at a temp folder, so
the listing/download/traversal/auth behaviour is exercised without any real
mount or udev. The actual read-only ``mount`` call lives in
``BrowseManager._do_mount`` and is field-validated on the device.
"""

from pathlib import Path

import pytest

from copystation.mounts import (
    BrowseManager,
    PathEscapesVolume,
    UnknownVolume,
    safe_resolve,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.config import Config  # noqa: E402
from copystation.state import StationState  # noqa: E402
from copystation.web.app import create_app  # noqa: E402


# --------------------------------------------------------------------------- #
# safe_resolve (pure path-traversal guard)
# --------------------------------------------------------------------------- #

def test_safe_resolve_allows_paths_inside_root(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "clip.mp4").write_bytes(b"x")
    assert safe_resolve(tmp_path, "") == Path(tmp_path).resolve()
    assert safe_resolve(tmp_path, "DCIM").name == "DCIM"
    assert safe_resolve(tmp_path, "/DCIM/clip.mp4").name == "clip.mp4"


def test_safe_resolve_rejects_parent_traversal(tmp_path):
    root = tmp_path / "mount"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope")
    for evil in ("../secret.txt", "..", "DCIM/../../secret.txt", "/../secret.txt"):
        with pytest.raises(PathEscapesVolume):
            safe_resolve(root, evil)


@pytest.mark.skipif(
    not hasattr(__import__("os"), "symlink"), reason="symlinks unsupported"
)
def test_safe_resolve_rejects_symlink_escape(tmp_path):
    import os

    root = tmp_path / "mount"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "loot.txt").write_text("secret")
    try:
        os.symlink(outside, root / "link")
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlink in this environment")
    with pytest.raises(PathEscapesVolume):
        safe_resolve(root, "link/loot.txt")


# --------------------------------------------------------------------------- #
# File endpoints via a fake BrowseManager
# --------------------------------------------------------------------------- #

class _FakeBrowse(BrowseManager):
    """BrowseManager that serves one temp folder as a fake USB volume."""

    def __init__(self, config, root, sys_name="sdb1"):
        super().__init__(config)
        self._root = Path(root)
        self._sys = sys_name

    def list_volumes(self):
        return [{"sys_name": self._sys, "device_node": "/dev/" + self._sys, "name": "TestCard"}]

    def _ensure_mounted(self, sys_name):
        if sys_name != self._sys:
            raise UnknownVolume(sys_name)
        return self._root


def _card(tmp_path):
    (tmp_path / "DCIM").mkdir()
    (tmp_path / "DCIM" / "clip.mp4").write_bytes(b"\x00" * 2048)
    (tmp_path / "notes.txt").write_text("hello card")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "inner.txt").write_text("deep")
    return tmp_path


def _client(browse, config=None):
    return TestClient(create_app(StationState(), config or Config(), browse=browse))


def test_volumes_lists_the_card(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/volumes")
    assert res.status_code == 200
    vols = res.json()["volumes"]
    assert [v["sys_name"] for v in vols] == ["sdb1"]
    assert vols[0]["name"] == "TestCard"


def test_files_lists_root_dirs_first(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files", params={"device": "sdb1", "path": ""})
    assert res.status_code == 200
    data = res.json()
    names = [e["name"] for e in data["entries"]]
    # Directories (DCIM, sub) sort before the file (notes.txt).
    assert names == ["DCIM", "sub", "notes.txt"]
    notes = next(e for e in data["entries"] if e["name"] == "notes.txt")
    assert notes["is_dir"] is False
    assert notes["size"] == len("hello card")


def test_files_lists_subdir(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files", params={"device": "sdb1", "path": "DCIM"})
    assert res.status_code == 200
    assert [e["name"] for e in res.json()["entries"]] == ["clip.mp4"]


def test_files_unknown_volume_is_404(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    assert client.get("/api/files", params={"device": "sdX", "path": ""}).status_code == 404


def test_files_missing_path_is_404(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files", params={"device": "sdb1", "path": "nope"})
    assert res.status_code == 404


def test_files_traversal_is_403(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files", params={"device": "sdb1", "path": "../.."})
    assert res.status_code == 403


def test_download_streams_file(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files/download", params={"device": "sdb1", "path": "notes.txt"})
    assert res.status_code == 200
    assert res.content == b"hello card"


def test_download_traversal_is_403(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get(
        "/api/files/download", params={"device": "sdb1", "path": "../../secret"}
    )
    assert res.status_code == 403


def test_download_directory_is_404(tmp_path):
    client = _client(_FakeBrowse(Config(), _card(tmp_path)))
    res = client.get("/api/files/download", params={"device": "sdb1", "path": "DCIM"})
    assert res.status_code == 404


def test_download_disabled_when_configured(tmp_path):
    cfg = Config()
    cfg.data["web"]["files"]["allow_download"] = False
    client = _client(_FakeBrowse(cfg, _card(tmp_path)), config=cfg)
    res = client.get("/api/files/download", params={"device": "sdb1", "path": "notes.txt"})
    assert res.status_code == 403


def test_file_routes_absent_without_browser():
    # No BrowseManager -> the file endpoints are not registered at all.
    client = TestClient(create_app(StationState(), Config(), browse=None))
    assert client.get("/api/files", params={"device": "sdb1"}).status_code == 404
    assert client.get("/api/settings").json()["features"]["files"] is False


def test_files_require_auth_when_enabled(tmp_path):
    cfg = Config()
    cfg.data["web"]["auth"] = {"enabled": True, "username": "admin", "password": "pw"}
    client = _client(_FakeBrowse(cfg, _card(tmp_path)), config=cfg)
    assert client.get("/api/volumes").status_code == 401
    assert client.get("/api/volumes", auth=("admin", "pw")).status_code == 200
