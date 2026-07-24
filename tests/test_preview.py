"""In-browser preview: classify a source as direct-playable or transcode-for-smooth.

ffprobe is faked, so this runs on the dev machine. Playback itself is the plain
file stream in the web layer; there is no live transcode to exercise here.
"""

from pathlib import Path

import pytest

import copystation.preview as pv
from copystation.mounts import NotFound, UnknownVolume
from copystation.preview import PreviewManager, PreviewUnavailable, preview_mode

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.config import Config  # noqa: E402
from copystation.state import StationState  # noqa: E402
from copystation.web.app import create_app  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure classification
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "info,expected",
    [
        # The hint is resolution-only now: codec and container never force it.
        ({"vcodec": "h264", "height": 1080, "container": "mp4"}, "direct"),
        ({"vcodec": "h264", "height": 720, "container": "mov"}, "direct"),
        ({"vcodec": "h264", "height": 1088, "container": "mp4"}, "direct"),      # 1080p coded padding
        ({"vcodec": "hevc", "height": 540, "container": "mkv"}, "direct"),       # small HEVC/mkv: no hint
        ({"vcodec": "hevc", "height": 1080, "container": "mp4"}, "direct"),      # codec ignored
        ({"vcodec": "h264", "height": 1080, "container": "mkv"}, "direct"),      # container ignored
        ({"vcodec": "h264", "height": 1440, "container": "mp4"}, "transcode"),   # QHD > Full HD
        ({"vcodec": "h264", "height": 2160, "container": "mp4"}, "transcode"),   # 4K
        ({"vcodec": "hevc", "height": 2160, "container": "mp4"}, "transcode"),   # 4K
        ({"vcodec": "h264", "height": 0, "container": "mp4"}, "direct"),         # unknown -> no hint
    ],
)
def test_preview_mode(info, expected):
    assert preview_mode(info) == expected


def test_preview_mode_honours_max_direct_height():
    info = {"vcodec": "h264", "height": 1440, "container": "mp4"}
    assert preview_mode(info, max_direct_height=1080) == "transcode"
    assert preview_mode(info, max_direct_height=1440) == "direct"


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #

class _FakeBrowse:
    def __init__(self, root):
        self.root = Path(root)
        self.allow_download = True

    def resolve_file(self, device, path):
        if device != "sdb1":
            raise UnknownVolume(device)
        p = self.root / path
        if not p.is_file():
            raise NotFound(path)
        return p


def _mgr(tmp_path, monkeypatch, info=None):
    card = tmp_path / "card"
    card.mkdir(exist_ok=True)
    (card / "clip.mp4").write_bytes(b"\x00" * 64)
    mgr = PreviewManager(Config(), _FakeBrowse(card))
    mgr._available = True
    monkeypatch.setattr(pv, "probe_video_info", lambda src: info or {
        "vcodec": "h264", "width": 3840, "height": 2160, "fps": 59.94,
        "duration": 10.0, "has_audio": False, "acodec": None, "container": "mp4"})
    return mgr


def test_info_reports_transcode_for_4k(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)  # 4K h264
    d = mgr.info("sdb1", "clip.mp4")
    assert d["mode"] == "transcode"
    assert d["width"] == 3840 and d["vcodec"] == "h264"


def test_info_reports_direct_for_1080p(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, info={
        "vcodec": "h264", "width": 1920, "height": 1080, "fps": 30.0,
        "duration": 10.0, "has_audio": True, "acodec": "aac", "container": "mp4"})
    assert mgr.info("sdb1", "clip.mp4")["mode"] == "direct"


def test_info_unknown_volume(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    with pytest.raises(UnknownVolume):
        mgr.info("sdX", "clip.mp4")


def test_info_unavailable(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._available = False
    with pytest.raises(PreviewUnavailable):
        mgr.info("sdb1", "clip.mp4")


# --------------------------------------------------------------------------- #
# Web endpoint
# --------------------------------------------------------------------------- #

def _client(tmp_path, monkeypatch, **kw):
    mgr = _mgr(tmp_path, monkeypatch, **kw)
    return TestClient(create_app(StationState(), Config(), browse=mgr._browse, preview=mgr))


def test_api_preview_info(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/settings").json()["features"]["preview"] is True
    r = client.get("/api/files/preview-info", params={"device": "sdb1", "path": "clip.mp4"})
    assert r.status_code == 200 and r.json()["mode"] == "transcode"


def test_api_preview_info_unknown_volume_is_404(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    assert client.get("/api/files/preview-info",
                      params={"device": "sdX", "path": "clip.mp4"}).status_code == 404


def test_preview_routes_absent_without_manager():
    client = TestClient(create_app(StationState(), Config(), browse=None, preview=None))
    assert client.get("/api/files/preview-info",
                      params={"device": "sdb1", "path": "x"}).status_code == 404
    assert client.get("/api/settings").json()["features"]["preview"] is False
