"""Video transcoding: pure ffmpeg command building, job bookkeeping and the API.

ffmpeg/ffprobe are never executed -- the subprocess is faked -- so this runs on
the dev machine. The real encode is field-validated on the device.
"""

from pathlib import Path

import pytest

import copystation.transcode as tc
from copystation.mounts import NotFound, UnknownVolume
from copystation.status import State, StatusIndicator
from copystation.transcode import (
    TranscodeBusy,
    TranscodeManager,
    TranscodeUnavailable,
    UnknownPreset,
    fits_in_ram,
    mem_available_bytes,
    output_name,
    progress_seconds,
    ram_budget,
    sanitize_component,
)

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.config import Config  # noqa: E402
from copystation.state import StationState, StatusHub  # noqa: E402
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
    """Models each device as a single directory (one mount for that device).

    Same device -> same directory for read (mount_ro) and write (mount_rw), which
    mirrors a real transcode reading the input from and writing the output to one
    read-write mount when input and output are on the same card. ``release`` and
    ``umount_rw`` calls are recorded so tests can assert the read-only browse mount
    is dropped before the read-write mount.
    """

    def __init__(self, roots=None):
        self.roots = {k: Path(v) for k, v in (roots or {}).items()}
        self.released = []
        self.umounted_rw = []
        self.ops = []  # ordered log of mount operations

    def resolve_input(self, device, path):
        if not self.roots:  # validation-only fakes (submit tests)
            if device != "sdb1":
                raise UnknownVolume(device)
            return Path("/fake") / path
        root = self.roots.get(device)
        if root is None:
            raise UnknownVolume(device)
        p = root / path
        if not p.is_file():
            raise NotFound(path)
        return p

    def mount_ro(self, device):
        self.ops.append(f"mount_ro:{device}")
        return self.roots[device]

    def mount_rw(self, device):
        self.ops.append(f"mount_rw:{device}")
        return self.roots[device]

    def umount_rw(self, device):
        self.ops.append(f"umount_rw:{device}")
        self.umounted_rw.append(device)

    def release(self, device):
        self.ops.append(f"release:{device}")
        self.released.append(device)


def _card(tmp_path, name="clip.mp4"):
    """A fake card directory (one device) with an input video in it."""
    card = tmp_path / "card"
    card.mkdir()
    (card / name).write_bytes(b"\x00" * 32)
    return card


def _hub():
    return StatusHub(StationState(), StatusIndicator())


def _mgr(browse, available=True, hub=None):
    mgr = TranscodeManager(Config(), hub or _hub(), browse)
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


def test_submit_blocked_while_copying(monkeypatch):
    hub = _hub()
    hub.state.set_phase(State.COPYING)
    mgr = _mgr(_FakeBrowse(), hub=hub)
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    with pytest.raises(TranscodeBusy):
        mgr.submit("sdb1", "clip.mp4", "720p-h264")


def test_submit_blocked_when_a_job_is_active(monkeypatch):
    mgr = _mgr(_FakeBrowse())
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    mgr.submit("sdb1", "clip.mp4", "720p-h264")  # queued
    with pytest.raises(TranscodeBusy):
        mgr.submit("sdb1", "clip.mp4", "720p-h264")  # second one refused


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
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
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
    assert (card / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"encoded"
    assert browse.umounted_rw == ["sdb1"]      # rw output volume released
    # The read-only browse mount is dropped BEFORE the read-write mount (the fix
    # for the same-device ro+rw superblock clash that made the output read-only).
    assert browse.ops.index("release:sdb1") < browse.ops.index("mount_rw:sdb1")
    # Same device -> no separate read-only input mount (read from the rw mount).
    assert "mount_ro:sdb1" not in browse.ops


def test_process_takes_over_phase_and_restores_it(tmp_path, monkeypatch):
    class _RecInd(StatusIndicator):
        def __init__(self):
            self.states = []

        def set_state(self, s):
            self.states.append(s)

    card = _card(tmp_path)
    ind = _RecInd()
    hub = StatusHub(StationState(), ind)
    hub.state.set_phase(State.DETECTING)  # a card was detected before the transcode
    mgr = _mgr(_FakeBrowse({"sdb1": card}), hub=hub)
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)

    class _Proc:
        def __init__(self, cmd, **kw):
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter(["out_time=00:00:05.000000\n", "progress=end\n"])

        def wait(self):
            self._dst.write_bytes(b"x")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    assert State.TRANSCODING in ind.states       # took over every indicator
    assert hub.state.phase is State.DETECTING     # ...and restored the phase after
    assert mgr.snapshot()["jobs"][0]["status"] == "done"


def test_process_different_devices_mount_each_side(tmp_path, monkeypatch):
    in_card = tmp_path / "in"
    in_card.mkdir()
    (in_card / "clip.mp4").write_bytes(b"\x00" * 32)
    out_card = tmp_path / "out"
    out_card.mkdir()
    browse = _FakeBrowse({"sdb1": in_card, "sdc1": out_card})
    mgr = _mgr(browse)
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 5.0)

    class _P:
        def __init__(self, cmd, **kw):
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self._dst.write_bytes(b"x")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _P)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264", output_device="sdc1")
    mgr._process(job["id"])

    assert mgr.snapshot()["jobs"][0]["status"] == "done"
    assert (out_card / "Transcoded" / "clip_720p-h264.mp4").exists()
    # Output device mounted read-write, input device read-only; both released.
    assert "mount_rw:sdc1" in browse.ops and "release:sdc1" in browse.ops
    assert "mount_ro:sdb1" in browse.ops and "release:sdb1" in browse.ops


def test_process_reports_ffmpeg_failure(tmp_path, monkeypatch):
    card = _card(tmp_path)
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
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
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)
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
    assert (card / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"cpu-encoded"


def test_cancel_during_encode_does_not_fall_back(tmp_path, monkeypatch):
    card = _card(tmp_path)
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
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
# RAM buffering (tmpfs staging)
# --------------------------------------------------------------------------- #

def test_mem_available_bytes_parses(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal:       4000000 kB\nMemAvailable:    2000000 kB\n")
    assert mem_available_bytes(str(f)) == 2_000_000 * 1024


def test_mem_available_bytes_missing_or_absent(tmp_path):
    f = tmp_path / "meminfo"
    f.write_text("MemTotal: 4000000 kB\n")
    assert mem_available_bytes(str(f)) == 0
    assert mem_available_bytes(str(tmp_path / "nope")) == 0


def test_ram_budget_and_fit():
    assert ram_budget(3000, 2 / 3) == 2000
    assert ram_budget(0, 0.5) == 0
    assert fits_in_ram(400, 1000) is True    # 2*400 <= 1000
    assert fits_in_ram(600, 1000) is False   # 2*600 > 1000
    assert fits_in_ram(0, 1000) is False
    assert fits_in_ram(100, 0) is False


def test_encode_buffers_through_ram_when_it_fits(tmp_path, monkeypatch):
    import shutil as _sh

    card = tmp_path / "card"
    card.mkdir()
    src = card / "clip.mp4"
    src.write_bytes(b"input" * 10)
    out_dir = card / "Transcoded"
    out_dir.mkdir()
    final_dst = out_dir / "clip_720p.mp4"

    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(mgr, "_ram_budget", lambda: 10 ** 9)  # plenty
    monkeypatch.setattr(mgr, "_work_base", str(tmp_path / "work"))
    monkeypatch.setattr(mgr, "_mount_tmpfs", lambda path, size: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mgr, "_umount_tmpfs", lambda path: _sh.rmtree(path, ignore_errors=True))

    seen = {}

    def fake_encode(job_id, s, d, preset):
        seen["src"] = Path(s)
        Path(d).write_bytes(b"encoded")

    monkeypatch.setattr(mgr, "_encode_with_fallback", fake_encode)

    mgr._encode(1, src, final_dst, {"id": "720p-h264"})

    # ffmpeg read/wrote in the RAM work dir (not the card), and the result was
    # copied back to the card.
    assert str(tmp_path / "work") in str(seen["src"])
    assert seen["src"].name == "input.mp4"
    assert final_dst.read_bytes() == b"encoded"


def test_encode_stays_on_card_when_input_too_large(tmp_path, monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    src = card / "clip.mp4"
    src.write_bytes(b"x" * 1000)
    out_dir = card / "Transcoded"
    out_dir.mkdir()
    final_dst = out_dir / "out.mp4"

    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(mgr, "_ram_budget", lambda: 100)  # < 2 * input -> no buffer
    monkeypatch.setattr(mgr, "_mount_tmpfs",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not mount")))

    seen = {}

    def fake_encode(job_id, s, d, preset):
        seen["src"] = Path(s)
        Path(d).write_bytes(b"e")

    monkeypatch.setattr(mgr, "_encode_with_fallback", fake_encode)

    mgr._encode(1, src, final_dst, {"id": "720p-h264"})
    assert seen["src"] == src  # encoded straight from the card
    assert final_dst.read_bytes() == b"e"


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
        if preset == "busy":
            raise TranscodeBusy("a copy is in progress")
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


def test_api_transcode_busy_is_409():
    client = _client(_FakeManager())
    res = client.post("/api/transcode", json={"device": "sdb1", "path": "c", "preset": "busy"})
    assert res.status_code == 409


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
