"""Video transcoding: pure ffmpeg command building, job bookkeeping and the API.

ffmpeg/ffprobe are never executed -- the subprocess is faked -- so this runs on
the dev machine. The real encode is field-validated on the device.
"""

from pathlib import Path

import pytest

import copystation.transcode as tc
from copystation.mounts import NotFound, UnknownVolume
from copystation.transcode import (
    TranscodeManager,
    TranscodeUnavailable,
    UnknownPreset,
    output_name,
    progress_seconds,
    sanitize_component,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.config import Config  # noqa: E402
from copystation.state import StationState  # noqa: E402
from copystation.web.app import create_app  # noqa: E402


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

def test_output_name_and_sanitize():
    assert output_name("DJI_0001.MP4", "720p-h264") == "DJI_0001_720p-h264.mp4"
    assert sanitize_component("../../etc/passwd") == "passwd"
    assert sanitize_component("weird name*?.mov") == "weird_name_.mov"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("out_time=00:00:05.000000", 5.0),
        ("out_time=00:01:30.500000", 90.5),
        ("out_time_us=2500000", 2.5),
        ("out_time_ms=2500000", 2.5),  # ffmpeg's _ms field is microseconds
        ("progress=continue", None),
        ("frame=42", None),
    ],
)
def test_progress_seconds(line, expected):
    assert progress_seconds(line) == expected


# --------------------------------------------------------------------------- #
# TranscodeManager submit validation
# --------------------------------------------------------------------------- #

class _FakeBrowse:
    def __init__(self, in_root=None, out_root=None):
        self.in_root = Path(in_root) if in_root else None
        self.out_root = Path(out_root) if out_root else None
        self.umounted = []

    def resolve_input(self, device, path):
        if device != "sdb1":
            raise UnknownVolume(device)
        if self.in_root is None:
            return Path("/fake") / path
        p = self.in_root / path
        if not p.is_file():
            raise NotFound(path)
        return p

    def mount_rw(self, device):
        return self.out_root

    def umount_rw(self, device):
        self.umounted.append(device)


def _mgr(browse, available=True):
    mgr = TranscodeManager(Config(), StationState(), browse)
    mgr._available = available
    return mgr


def test_submit_requires_ffmpeg():
    mgr = _mgr(_FakeBrowse(), available=False)
    with pytest.raises(TranscodeUnavailable):
        mgr.submit("sdb1", "clip.mp4", "720p-h264")


def test_submit_unknown_preset(monkeypatch):
    mgr = _mgr(_FakeBrowse())
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    with pytest.raises(UnknownPreset):
        mgr.submit("sdb1", "clip.mp4", "nope")


def test_submit_unknown_volume(monkeypatch):
    mgr = _mgr(_FakeBrowse())
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    with pytest.raises(UnknownVolume):
        mgr.submit("sdX", "clip.mp4", "720p-h264")


def test_submit_queues_job(monkeypatch):
    mgr = _mgr(_FakeBrowse())
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    assert job["status"] == "queued"
    snap = mgr.snapshot()
    assert snap["available"] is True
    assert snap["jobs"][0]["id"] == job["id"]
    assert "720p-h264" in [p["id"] for p in snap["presets"]]


def test_cancel_queued_job(monkeypatch):
    mgr = _mgr(_FakeBrowse())
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    assert mgr.cancel(job["id"]) is True
    assert mgr.cancel(job["id"]) is False  # already canceled
    assert mgr.cancel(999) is False


# --------------------------------------------------------------------------- #
# Worker orchestration with a faked ffmpeg process
# --------------------------------------------------------------------------- #

def test_process_runs_ffmpeg_and_writes_output(tmp_path, monkeypatch):
    in_root = tmp_path / "in"
    in_root.mkdir()
    (in_root / "clip.mp4").write_bytes(b"\x00" * 32)
    out_root = tmp_path / "out"
    out_root.mkdir()
    browse = _FakeBrowse(in_root=in_root, out_root=out_root)
    mgr = _mgr(browse)
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)

    class _FakeProc:
        def __init__(self, cmd, **kw):
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter(["out_time=00:00:05.000000\n", "progress=end\n"])

        def wait(self):
            self._dst.write_bytes(b"encoded")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _FakeProc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)  # sync

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert result["percent"] == 100
    assert result["output_path"] == "Transcoded/clip_720p-h264.mp4"
    assert (out_root / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"encoded"
    assert browse.umounted == ["sdb1"]  # rw output volume released


def test_process_reports_ffmpeg_failure(tmp_path, monkeypatch):
    in_root = tmp_path / "in"
    in_root.mkdir()
    (in_root / "clip.mp4").write_bytes(b"\x00" * 32)
    out_root = tmp_path / "out"
    out_root.mkdir()
    mgr = _mgr(_FakeBrowse(in_root=in_root, out_root=out_root))
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: None)

    class _FailProc:
        def __init__(self, cmd, **kw):
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self.returncode = 1

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _FailProc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])
    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "error"
    assert "code 1" in result["error"]


def test_process_falls_back_to_cpu_when_hardware_fails(tmp_path, monkeypatch):
    in_root = tmp_path / "in"
    in_root.mkdir()
    (in_root / "clip.mp4").write_bytes(b"\x00" * 32)
    out_root = tmp_path / "out"
    out_root.mkdir()
    mgr = _mgr(_FakeBrowse(in_root=in_root, out_root=out_root))
    # Simulate a Pi 4 whose ffmpeg has the H.264 hardware encoder.
    mgr._board = "pi4"
    mgr._encoders_avail = {"h264_v4l2m2m", "libx264"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)

    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self._hw = "h264_v4l2m2m" in cmd
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            if self._hw:
                self.returncode = 1  # hardware encode fails at runtime
            else:
                self._dst.write_bytes(b"cpu-encoded")
                self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert result["encoder"] == "cpu"
    assert result["hw"] is False
    # Two attempts: hardware first, then the CPU fallback.
    assert len(calls) == 2
    assert "h264_v4l2m2m" in calls[0]
    assert "libx264" in calls[1]
    assert (out_root / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"cpu-encoded"


def test_cancel_during_encode_does_not_fall_back(tmp_path, monkeypatch):
    in_root = tmp_path / "in"
    in_root.mkdir()
    (in_root / "clip.mp4").write_bytes(b"\x00" * 32)
    out_root = tmp_path / "out"
    out_root.mkdir()
    mgr = _mgr(_FakeBrowse(in_root=in_root, out_root=out_root))
    mgr._board = "pi4"
    mgr._encoders_avail = {"h264_v4l2m2m", "libx264"}  # a fallback exists...
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)

    calls = []
    holder = {}

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            # A cancel arrives while the (first) encoder runs.
            mgr._jobs[holder["id"]]["status"] = "canceled"
            self.returncode = -15

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    holder["id"] = job["id"]
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "canceled"
    assert len(calls) == 1  # ...but a cancel must NOT trigger the fallback


# --------------------------------------------------------------------------- #
# Web endpoints via a fake manager
# --------------------------------------------------------------------------- #

class _FakeManager:
    def __init__(self, available=True):
        self._available = available
        self.jobs = []

    def snapshot(self):
        return {
            "available": self._available,
            "output_dirname": "Transcoded",
            "presets": [{"id": "720p-h264", "label": "720p H.264"}],
            "jobs": self.jobs,
        }

    def submit(self, device, path, preset, output_device=None):
        if not self._available:
            raise TranscodeUnavailable("no ffmpeg")
        if preset == "bad":
            raise UnknownPreset("bad")
        if device == "sdX":
            raise UnknownVolume("sdX")
        job = {"id": 7, "status": "queued", "preset": preset,
               "input_device": device, "input_path": path}
        self.jobs = [job]
        return job

    def cancel(self, job_id):
        return job_id == 7


def _client(transcode):
    return TestClient(create_app(StationState(), Config(), browse=None, transcode=transcode))


def test_api_transcode_status_and_submit():
    client = _client(_FakeManager())
    assert client.get("/api/settings").json()["features"]["transcode"] is True
    snap = client.get("/api/transcode")
    assert snap.status_code == 200
    assert snap.json()["presets"][0]["id"] == "720p-h264"

    res = client.post("/api/transcode", json={"device": "sdb1", "path": "clip.mp4", "preset": "720p-h264"})
    assert res.status_code == 200
    assert res.json()["id"] == 7


def test_api_transcode_errors():
    client = _client(_FakeManager())
    assert client.post("/api/transcode", json={"device": "sdb1", "path": "c", "preset": "bad"}).status_code == 400
    assert client.post("/api/transcode", json={"device": "sdX", "path": "c", "preset": "720p-h264"}).status_code == 404


def test_api_transcode_unavailable_is_501():
    client = _client(_FakeManager(available=False))
    res = client.post("/api/transcode", json={"device": "sdb1", "path": "c", "preset": "720p-h264"})
    assert res.status_code == 501


def test_api_transcode_cancel():
    client = _client(_FakeManager())
    assert client.delete("/api/transcode/7").status_code == 200
    assert client.delete("/api/transcode/8").status_code == 404


def test_transcode_routes_absent_without_manager():
    client = TestClient(create_app(StationState(), Config(), transcode=None))
    assert client.get("/api/transcode").status_code == 404
    assert client.get("/api/settings").json()["features"]["transcode"] is False
