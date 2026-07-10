"""On-the-fly HLS preview: pure playlist/command building and the manager/API.

ffmpeg/GStreamer are never executed -- the subprocess is faked -- so this runs on
the dev machine. The real (hardware) encode is field-validated on the device.
"""

import io
from pathlib import Path

import pytest

import copystation.preview as pv
from copystation.mounts import NotFound, UnknownVolume
from copystation.preview import (
    PreviewBusy,
    PreviewManager,
    PreviewUnavailable,
    build_ffmpeg_segment_cmd,
    build_hw_segment_cmds,
    build_playlist,
    preview_mode,
    segment_count,
)
from copystation.status import State

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.config import Config  # noqa: E402
from copystation.state import StationState  # noqa: E402
from copystation.web.app import create_app  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_segment_count():
    assert segment_count(10, 4) == 3
    assert segment_count(12, 4) == 3
    assert segment_count(13, 4) == 4
    assert segment_count(0, 4) == 0
    assert segment_count(10, 0) == 0


def test_build_playlist_is_seekable_vod():
    m = build_playlist(10.0, 4.0, "device=sdb1&path=x.mp4")
    assert m.startswith("#EXTM3U")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in m       # seekable
    assert "#EXT-X-ENDLIST" in m
    assert m.count("#EXTINF:") == 3              # 4 + 4 + 2 seconds
    assert "seg-0.ts?device=sdb1&path=x.mp4" in m
    assert "seg-2.ts?device=sdb1&path=x.mp4" in m
    assert "#EXTINF:2.000," in m                 # short last segment


@pytest.mark.parametrize(
    "info,expected",
    [
        ({"vcodec": "h264", "height": 1080, "container": "mp4"}, "direct"),
        ({"vcodec": "h264", "height": 720, "container": "mov"}, "direct"),
        ({"vcodec": "h264", "height": 2160, "container": "mp4"}, "hls"),   # 4K
        ({"vcodec": "hevc", "height": 1080, "container": "mp4"}, "hls"),   # HEVC
        ({"vcodec": "h264", "height": 1080, "container": "mkv"}, "hls"),   # container
        ({"vcodec": "vp9", "height": 720, "container": "webm"}, "hls"),
    ],
)
def test_preview_mode(info, expected):
    assert preview_mode(info) == expected


def test_build_ffmpeg_segment_cmd():
    cmd = build_ffmpeg_segment_cmd("/card/x.mp4", 8.0, 4.0, 1080, 30, "8M")
    assert cmd[0] == "ffmpeg" and cmd[-1] == "pipe:1"
    # fast input seek (before -i), bounded, downscaled and fps-capped
    assert cmd[cmd.index("-ss") + 1] == "8.000"
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-t") + 1] == "4.000"
    assert "scale=-2:1080" in cmd
    assert cmd[cmd.index("-r") + 1] == "30"
    assert "mpegts" in cmd


def test_build_hw_segment_cmds_pipes_ffmpeg_seek_into_gstreamer():
    info = {"vcodec": "h264", "height": 2160, "has_audio": True, "acodec": "aac"}
    ff, gst = build_hw_segment_cmds("/card/x.mp4", info, 8.0, 4.0, 1080, 30, "8M")
    # ffmpeg does the seek + stream-copy (no decode) to mpegts on stdout
    assert ff[0] == "ffmpeg" and "copy" in ff and ff[-1] == "pipe:1"
    assert ff.index("-ss") < ff.index("-i")
    # gstreamer reads that on stdin and hardware decode/scale/encode -> mpegts
    assert gst[0] == "gst-launch-1.0"
    assert "fdsrc" in gst and "omxh264dec" in gst and "omxh264videoenc" in gst
    assert "fdsink" in gst
    assert "aacparse" in gst  # AAC audio carried through
    # The downscale is in the DECODER (scale property); NO CPU element between the
    # OMX decoder and encoder (that deadlocks/SIGSEGVs the shared VPU buffer pool).
    assert "scale=1" in gst  # 4K -> 1080p is an exact 1/2 decoder downscale
    assert "videoscale" not in gst and "videorate" not in gst


def test_build_hw_segment_cmds_hevc_and_no_audio():
    info = {"vcodec": "hevc", "height": 2160, "has_audio": False, "acodec": None}
    ff, gst = build_hw_segment_cmds("/card/x.mp4", info, 0.0, 4.0, 1080, 30, "8M")
    assert "-an" in ff                    # drop audio
    assert "omxhevcvideodec" in gst       # HEVC hardware decoder
    assert "scale=1" in gst
    assert "aacparse" not in gst


# --------------------------------------------------------------------------- #
# Manager (faked ffmpeg/GStreamer)
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


class _FakeProc:
    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.kw = kw
        self.stdout = io.BytesIO(b"TS" * 5000)
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def kill(self):  # pragma: no cover - defensive
        self.returncode = -9


def _mgr(tmp_path, monkeypatch, info=None, board="cubie", encoders=("libx264",)):
    card = tmp_path / "card"
    card.mkdir(exist_ok=True)
    (card / "clip.mp4").write_bytes(b"\x00" * 64)
    mgr = PreviewManager(Config(), _FakeBrowse(card), StationState())
    mgr._available = True
    mgr._board = board
    mgr._encoders_avail = set(encoders)
    monkeypatch.setattr(pv, "probe_video_info", lambda src: info or {
        "vcodec": "h264", "width": 3840, "height": 2160, "fps": 59.94,
        "duration": 10.0, "has_audio": False, "acodec": None, "container": "mp4"})
    monkeypatch.setattr(pv, "probe_duration", lambda src: 10.0)
    return mgr


def test_info_reports_mode(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)  # 4K -> hls
    assert mgr.info("sdb1", "clip.mp4")["mode"] == "hls"
    mgr2 = _mgr(tmp_path, monkeypatch, info={
        "vcodec": "h264", "width": 1920, "height": 1080, "fps": 30.0,
        "duration": 10.0, "has_audio": False, "acodec": None, "container": "mp4"})
    assert mgr2.info("sdb1", "clip.mp4")["mode"] == "direct"


def test_playlist(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    m = mgr.playlist("sdb1", "clip.mp4")
    assert "#EXT-X-ENDLIST" in m and "seg-0.ts?device=sdb1&path=clip.mp4" in m


def test_playlist_unknown_volume(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    with pytest.raises(UnknownVolume):
        mgr.playlist("sdX", "clip.mp4")


def test_iter_segment_streams_and_cleans_up(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, board="pi5")  # CPU path -> one process
    spawned = []
    orig = _FakeProc

    class _Rec(_FakeProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            spawned.append(self)

    monkeypatch.setattr(pv.subprocess, "Popen", _Rec)
    data = b"".join(mgr.iter_segment("sdb1", "clip.mp4", 0))
    assert data == b"TS" * 5000
    assert len(spawned) == 1 and spawned[0].returncode == 0  # torn down


def test_iter_segment_uses_hardware_on_cubie(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, board="cubie",
               encoders=("omxh264videoenc", "omxh264dec", "libx264"))
    spawned = []

    class _Rec(_FakeProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            spawned.append(cmd[0])

    monkeypatch.setattr(pv.subprocess, "Popen", _Rec)
    b"".join(mgr.iter_segment("sdb1", "clip.mp4", 2))
    assert spawned == ["ffmpeg", "gst-launch-1.0"]  # ffmpeg-seek | gst-omx pipe


def test_iter_segment_hevc_falls_back_to_cpu_on_cubie(tmp_path, monkeypatch):
    # HEVC decode loses framerate on the OMX path (broken bitrate), so an HEVC
    # source uses the CPU ffmpeg path even on the Cubie.
    mgr = _mgr(tmp_path, monkeypatch, board="cubie",
               encoders=("omxh264videoenc", "omxhevcvideodec", "libx264"),
               info={"vcodec": "hevc", "width": 3840, "height": 2160, "fps": 100.0,
                     "duration": 10.0, "has_audio": False, "acodec": None,
                     "container": "mp4"})
    spawned = []

    class _Rec(_FakeProc):
        def __init__(self, cmd, **kw):
            super().__init__(cmd, **kw)
            spawned.append(cmd[0])

    monkeypatch.setattr(pv.subprocess, "Popen", _Rec)
    b"".join(mgr.iter_segment("sdb1", "clip.mp4", 0))
    assert spawned == ["ffmpeg"]  # single CPU ffmpeg, not the gst-omx pipe


def test_iter_segment_refused_when_busy(tmp_path, monkeypatch):
    state = StationState()
    state.set_phase(State.TRANSCODING)
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._state = state
    with pytest.raises(PreviewBusy):
        list(mgr.iter_segment("sdb1", "clip.mp4", 0))


def test_iter_segment_unavailable(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._available = False
    with pytest.raises(PreviewUnavailable):
        list(mgr.iter_segment("sdb1", "clip.mp4", 0))


# --------------------------------------------------------------------------- #
# Proxy preview (transcode-once, then play the file)
# --------------------------------------------------------------------------- #

class _EncProc(_FakeProc):
    """Fake transcoder: writes the output file its command names, exits 0."""

    def __init__(self, cmd, **kw):
        super().__init__(cmd, **kw)
        out = next((a.split("=", 1)[1] for a in cmd if a.startswith("location=")), cmd[-1])
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"PROXY-MP4")
        self.stdout = io.StringIO("out_time=00:00:05.000000\nprogress=end\n")
        self.returncode = 0

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


def _wait_ready(mgr, device="sdb1", path="clip.mp4", tries=100):
    import time as _t
    for _ in range(tries):
        s = mgr.proxy_status(device, path)
        if s["state"] in ("ready", "error"):
            return s
        _t.sleep(0.02)
    return mgr.proxy_status(device, path)


def test_proxy_status_transcodes_then_ready(tmp_path, monkeypatch):
    mgr = _mgr(tmp_path, monkeypatch, board="pi5")  # CPU ffmpeg proxy
    mgr._cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pv.subprocess, "Popen", _EncProc)

    first = mgr.proxy_status("sdb1", "clip.mp4")
    assert first["state"] in ("transcoding", "ready")
    s = _wait_ready(mgr)
    assert s["state"] == "ready" and s["url"].endswith(".mp4")
    # the finished proxy is served by proxy_file
    key = s["url"].rsplit("/", 1)[1][:-4]
    assert mgr.proxy_file(key).read_bytes() == b"PROXY-MP4"
    # a second request returns the cached result (no new transcode)
    assert mgr.proxy_status("sdb1", "clip.mp4")["state"] == "ready"


def test_proxy_status_refused_when_busy(tmp_path, monkeypatch):
    state = StationState()
    state.set_phase(State.COPYING)
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._state = state
    mgr._cache_dir = tmp_path / "cache"
    with pytest.raises(PreviewBusy):
        mgr.proxy_status("sdb1", "clip.mp4")


def test_proxy_file_rejects_bad_key(tmp_path, monkeypatch):
    from copystation.mounts import BrowseError, NotFound
    mgr = _mgr(tmp_path, monkeypatch)
    mgr._cache_dir = tmp_path / "cache"
    with pytest.raises(BrowseError):
        mgr.proxy_file("../etc/passwd")       # not a 16-hex key -> rejected
    with pytest.raises(NotFound):
        mgr.proxy_file("0123456789abcdef")     # valid shape but absent


# --------------------------------------------------------------------------- #
# Web endpoints
# --------------------------------------------------------------------------- #

def _client(tmp_path, monkeypatch, **kw):
    mgr = _mgr(tmp_path, monkeypatch, **kw)
    monkeypatch.setattr(pv.subprocess, "Popen", _FakeProc)
    app = create_app(StationState(), Config(), browse=mgr._browse, preview=mgr)
    return TestClient(app), mgr


def test_api_preview_info_and_playlist_and_segment(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, board="pi5")
    assert client.get("/api/settings").json()["features"]["preview"] is True

    info = client.get("/api/files/preview-info", params={"device": "sdb1", "path": "clip.mp4"})
    assert info.status_code == 200 and info.json()["mode"] == "hls"

    m = client.get("/api/files/preview/index.m3u8", params={"device": "sdb1", "path": "clip.mp4"})
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    assert "#EXT-X-ENDLIST" in m.text

    seg = client.get("/api/files/preview/seg-0.ts", params={"device": "sdb1", "path": "clip.mp4"})
    assert seg.status_code == 200
    assert seg.headers["content-type"] == "video/mp2t"
    assert seg.content == b"TS" * 5000


def test_api_preview_segment_busy_is_409(tmp_path, monkeypatch):
    client, mgr = _client(tmp_path, monkeypatch, board="pi5")
    mgr._state.set_phase(State.COPYING)
    seg = client.get("/api/files/preview/seg-0.ts", params={"device": "sdb1", "path": "clip.mp4"})
    assert seg.status_code == 409


def test_api_preview_unknown_volume_is_404(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch, board="pi5")
    assert client.get("/api/files/preview-info",
                      params={"device": "sdX", "path": "clip.mp4"}).status_code == 404
    assert client.get("/api/files/preview/seg-0.ts",
                      params={"device": "sdX", "path": "clip.mp4"}).status_code == 404


def test_api_preview_proxy_flow(tmp_path, monkeypatch):
    import time as _t
    mgr = _mgr(tmp_path, monkeypatch, board="pi5")
    mgr._cache_dir = tmp_path / "cache"
    monkeypatch.setattr(pv.subprocess, "Popen", _EncProc)
    client = TestClient(create_app(StationState(), Config(), browse=mgr._browse, preview=mgr))

    s = client.get("/api/files/preview-proxy", params={"device": "sdb1", "path": "clip.mp4"}).json()
    assert s["state"] in ("transcoding", "ready")
    for _ in range(100):
        s = client.get("/api/files/preview-proxy", params={"device": "sdb1", "path": "clip.mp4"}).json()
        if s["state"] == "ready":
            break
        _t.sleep(0.02)
    assert s["state"] == "ready" and s["url"].endswith(".mp4")

    served = client.get(s["url"])
    assert served.status_code == 200
    assert served.content == b"PROXY-MP4"
    assert served.headers["content-type"].startswith("video/mp4")
    # cancel endpoint is well-formed (job already finished -> canceled: false)
    assert client.delete("/api/files/preview-proxy",
                         params={"device": "sdb1", "path": "clip.mp4"}).status_code == 200


def test_preview_routes_absent_without_manager():
    client = TestClient(create_app(StationState(), Config(), browse=None, preview=None))
    assert client.get("/api/files/preview-info",
                      params={"device": "sdb1", "path": "x"}).status_code == 404
    assert client.get("/api/files/preview-proxy",
                      params={"device": "sdb1", "path": "x"}).status_code == 404
    assert client.get("/api/settings").json()["features"]["preview"] is False
