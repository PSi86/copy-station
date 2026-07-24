"""Video transcoding: pure ffmpeg command building, job bookkeeping and the API.

ffmpeg/ffprobe are never executed -- the subprocess is faked -- so this runs on
the dev machine. The real encode is field-validated on the device.
"""

from pathlib import Path

import pytest

import copystation.transcode as tc
from copystation.mounts import NotFound, PathEscapesVolume, UnknownVolume
from copystation.status import State, StatusIndicator
from copystation.transcode import (
    InvalidSetting,
    TranscodeBusy,
    TranscodeManager,
    TranscodeUnavailable,
    UnknownPreset,
    estimate_output_bytes,
    estimate_seconds,
    gst_progress_percent,
    gst_progress_position,
    mem_available_bytes,
    output_name,
    parse_bitrate,
    perf_key,
    probe_video_info,
    progress_seconds,
    ram_budget,
    sanitize_component,
    unique_output_path,
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


def test_unique_output_path(tmp_path):
    p = tmp_path / "clip_720p-h264.mp4"
    assert unique_output_path(p) == p          # free name -> unchanged
    p.write_bytes(b"one")
    assert unique_output_path(p) == tmp_path / "clip_720p-h264_2.mp4"
    (tmp_path / "clip_720p-h264_2.mp4").write_bytes(b"two")
    assert unique_output_path(p) == tmp_path / "clip_720p-h264_3.mp4"


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


@pytest.mark.parametrize(
    "line,expected",
    [
        ("progressreport0 (00:00:12): 7 / 56 seconds (12.5 %)", 12.5),
        ("progressreport0 (00:00:02): 1 / 56 seconds ( 1.8 %)", 1.8),
        ("progressreport0 (00:00:56): 56 / 56 seconds (100.0 %)", 100.0),
        ("Setting pipeline to PLAYING ...", None),
        ("some ( bogus ) line", None),
    ],
)
def test_gst_progress_percent(line, expected):
    assert gst_progress_percent(line) == expected


@pytest.mark.parametrize(
    "line,expected",
    [
        ("progressreport0 (00:00:12): 7 / 56 seconds (12.5 %)", 7),
        ("progressreport0 (00:00:40): 23 / 23 seconds (100.0 %)", 23),
        ("Setting pipeline to PLAYING ...", None),
    ],
)
def test_gst_progress_position(line, expected):
    assert gst_progress_position(line) == expected


def test_probe_video_info_skips_attached_pic_and_finds_audio(monkeypatch):
    import json as _json
    payload = {
        "streams": [
            # embedded thumbnail first -> must be skipped, not chosen as the video
            {"codec_type": "video", "codec_name": "mjpeg", "width": 1280, "height": 720,
             "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": "h264", "width": 3840, "height": 2160,
             "avg_frame_rate": "60000/1001", "r_frame_rate": "60000/1001",
             "disposition": {"attached_pic": 0}},
            {"codec_type": "audio", "codec_name": "aac"},
        ],
        "format": {"duration": "56.89"},
    }

    class _R:
        stdout = _json.dumps(payload)

    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: _R())
    info = probe_video_info("clip.MP4")
    assert info["vcodec"] == "h264"
    assert (info["width"], info["height"]) == (3840, 2160)
    assert round(info["fps"], 2) == 59.94
    assert info["duration"] == 56.89
    assert info["has_audio"] is True and info["acodec"] == "aac"
    assert info["container"] == "mp4"


def test_probe_video_info_defaults_on_error(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("no ffprobe")

    monkeypatch.setattr(tc.subprocess, "run", boom)
    info = probe_video_info("clip.mkv")
    assert info["vcodec"] is None and info["container"] == "mkv"


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

    def list_dir(self, device, rel):
        root = self.roots.get(device)
        if root is None:
            raise UnknownVolume(device)
        target = (root / rel) if rel else root
        if not target.is_dir():
            raise NotFound(rel)
        entries = []
        for child in sorted(target.iterdir()):
            is_dir = child.is_dir()
            entries.append({
                "name": child.name,
                "is_dir": is_dir,
                "size": None if is_dir else child.stat().st_size,
                "mtime": child.stat().st_mtime,
            })
        return {"device": device, "path": rel or "", "entries": entries}

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
    # The done job reports the TRANSCODED file's size (not the source's).
    assert result["output_size"] == len(b"encoded")
    assert result["ram_buffered"] is False  # tmpfs not used on the dev machine
    assert browse.umounted_rw == ["sdb1"]      # rw output volume released
    # The read-only browse mount is dropped BEFORE the read-write mount (the fix
    # for the same-device ro+rw superblock clash that made the output read-only).
    assert browse.ops.index("release:sdb1") < browse.ops.index("mount_rw:sdb1")
    # Same device -> no separate read-only input mount (read from the rw mount).
    assert "mount_ro:sdb1" not in browse.ops


def test_process_does_not_overwrite_existing_output(tmp_path, monkeypatch):
    # A pre-existing output of the same name (e.g. from another source in a batch)
    # must survive: the new job is written to <stem>_2 instead of clobbering it.
    card = _card(tmp_path)
    existing = card / "Transcoded" / "clip_720p-h264.mp4"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"KEEP-ME")
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
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert existing.read_bytes() == b"KEEP-ME"  # original untouched
    assert result["output_path"] == "Transcoded/clip_720p-h264_2.mp4"
    assert result["filename"] == "clip_720p-h264_2.mp4"
    assert (card / "Transcoded" / "clip_720p-h264_2.mp4").read_bytes() == b"encoded"


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


class _WriteProc:
    """A faked encoder subprocess that just writes its output file and exits 0."""

    def __init__(self, cmd, **kw):
        self._dst = Path(cmd[-1])
        self.returncode = None
        self.stdout = iter([])

    def wait(self):
        self._dst.write_bytes(b"x")
        self.returncode = 0

    def terminate(self):  # pragma: no cover
        pass


def _nested_card(tmp_path):
    """A fake card with the source video one directory down (DCIM/clip.mp4)."""
    card = tmp_path / "card"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "clip.mp4").write_bytes(b"\x00" * 32)
    return card


def test_output_location_same_writes_beside_source(tmp_path, monkeypatch):
    card = _nested_card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)  # the default output location is "same"
    assert mgr.output_location == "same"
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 5.0)
    monkeypatch.setattr(tc.subprocess, "Popen", _WriteProc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "DCIM/clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert result["output_device"] == "sdb1"  # always the source's own medium
    assert result["output_path"] == "DCIM/Transcoded/clip_720p-h264.mp4"
    assert (card / "DCIM" / "Transcoded" / "clip_720p-h264.mp4").exists()
    # The source's own device is mounted read-write once; no separate read-only mount.
    assert "mount_rw:sdb1" in browse.ops and "umount_rw:sdb1" in browse.ops
    assert not any(op.startswith("mount_ro:") for op in browse.ops)


def test_output_location_central_writes_at_volume_root(tmp_path, monkeypatch):
    card = _nested_card(tmp_path)
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    mgr._output_location = "central"
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 5.0)
    monkeypatch.setattr(tc.subprocess, "Popen", _WriteProc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "DCIM/clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["output_path"] == "Transcoded/clip_720p-h264.mp4"
    assert (card / "Transcoded" / "clip_720p-h264.mp4").exists()


def test_process_reports_ffmpeg_failure(tmp_path, monkeypatch):
    card = _card(tmp_path)
    hub = _hub()
    mgr = _mgr(_FakeBrowse({"sdb1": card}), hub=hub)
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
    # The failure is surfaced on every backend via the ERROR phase (not hidden by
    # restoring the previous phase), and the transcode block is cleared.
    assert hub.state.phase is State.ERROR
    assert hub.state.snapshot()["transcode"]["active"] is False


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


def _pi5_mgr(tmp_path, monkeypatch, vcodec="hevc", src_wh=(3840, 2160)):
    """A manager posing as a Pi 5 (no HW encoder; HEVC decode via -hwaccel drm)."""
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)
    mgr._board = "pi5"
    mgr._encoders_avail = {"libx264", "libx265"}
    mgr._hwaccels = {"drm"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": vcodec, "width": src_wh[0], "height": src_wh[1], "fps": 59.94,
        "has_audio": False, "acodec": None, "container": "mp4"})
    return mgr, card


def test_process_pi5_hevc_offloads_decode_to_hardware(tmp_path, monkeypatch):
    # A Pi 5 has no HW encoder, but hardware-decodes HEVC: the CPU encode runs with
    # -hwaccel drm so the 4K decode is offloaded. One attempt, no fallback needed.
    mgr, card = _pi5_mgr(tmp_path, monkeypatch, vcodec="hevc")
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self._dst.write_bytes(b"hwdec-encoded")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert len(calls) == 1
    assert calls[0][0] == "ffmpeg" and "libx264" in calls[0]
    assert "-hwaccel" in calls[0] and calls[0][calls[0].index("-hwaccel") + 1] == "drm"
    assert calls[0].index("-hwaccel") < calls[0].index("-i")
    # It is a CPU *encode* (hw=False), but the label notes the hardware decode.
    assert result["hw"] is False
    assert result["encoder"] == "cpu (hevc hw-decode)"
    assert (card / "Transcoded" / "clip_1080p-h264.mp4").read_bytes() == b"hwdec-encoded"


def test_process_pi5_hevc_falls_back_to_software_decode(tmp_path, monkeypatch):
    # If the hardware decode fails at runtime, it retries with plain software decode
    # (same CPU encoder, no -hwaccel) rather than failing the job.
    mgr, card = _pi5_mgr(tmp_path, monkeypatch, vcodec="hevc")
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self._hwaccel = "-hwaccel" in cmd
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            if self._hwaccel:
                self.returncode = 1  # the hw-decode attempt fails
            else:
                self._dst.write_bytes(b"swdec-encoded")
                self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert len(calls) == 2                     # hw-decode attempt, then software
    assert "-hwaccel" in calls[0] and "-hwaccel" not in calls[1]
    assert result["encoder"] == "cpu" and result["hw"] is False
    assert (card / "Transcoded" / "clip_1080p-h264.mp4").read_bytes() == b"swdec-encoded"


def test_process_pi5_h264_stays_on_software_decode(tmp_path, monkeypatch):
    # H.264 has no hardware decoder on the Pi 5 -> a plain CPU transcode, no hwaccel.
    mgr, card = _pi5_mgr(tmp_path, monkeypatch, vcodec="h264")
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self._dst.write_bytes(b"cpu")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert len(calls) == 1 and "-hwaccel" not in calls[0]
    assert result["encoder"] == "cpu" and result["hw"] is False


def test_process_pi5_hevc_no_hwaccel_when_acceleration_cpu(tmp_path, monkeypatch):
    # acceleration: cpu means "no hardware at all" -> decode offload is skipped too.
    mgr, card = _pi5_mgr(tmp_path, monkeypatch, vcodec="hevc")
    mgr._acceleration = "cpu"
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self._dst.write_bytes(b"cpu")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert len(calls) == 1 and "-hwaccel" not in calls[0]
    assert result["encoder"] == "cpu"


def test_process_pi4_hevc_uses_hw_decode_plus_hw_encode(tmp_path, monkeypatch):
    # A Pi 4 has BOTH the HEVC hardware decoder and the H.264 hardware encoder, so
    # an HEVC source is decoded (-hwaccel drm) AND encoded (h264_v4l2m2m) in one
    # hardware pass -- the first candidate, so a single successful attempt.
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)
    mgr._board = "pi4"
    mgr._encoders_avail = {"h264_v4l2m2m", "libx264"}
    mgr._hwaccels = {"drm"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": "hevc", "width": 3840, "height": 2160, "fps": 59.94,
        "has_audio": False, "acodec": None, "container": "mp4"})
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
            self._dst.write_bytes(b"hw-decode-hw-encode")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert len(calls) == 1                       # HW-decode + HW-encode succeeds first
    assert "-hwaccel" in calls[0] and calls[0][calls[0].index("-hwaccel") + 1] == "drm"
    assert calls[0][calls[0].index("-c:v") + 1] == "h264_v4l2m2m"
    assert result["hw"] is True                  # a hardware *encode* this time
    assert result["encoder"] == "h264_v4l2m2m (hevc hw-decode)"
    assert (card / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"hw-decode-hw-encode"


def _cubie_mgr(tmp_path, monkeypatch, src_wh=(3840, 2160)):
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)
    mgr._board = "cubie"
    mgr._encoders_avail = {"omxh264videoenc", "omxh264dec", "libx264"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": "h264", "width": src_wh[0], "height": src_wh[1], "fps": 59.94,
        "has_audio": False, "acodec": None, "container": "mp4"})
    return mgr, card


def test_process_cubie_uses_gstreamer_hardware(tmp_path, monkeypatch):
    # 4K -> 1080p is an exact 1/2 step: a single hardware GStreamer pass.
    mgr, card = _cubie_mgr(tmp_path, monkeypatch)
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1].split("location=", 1)[-1])
            self.returncode = None
            self.stdout = iter(["progressreport0 (00:00:05): 5 / 10 seconds (50.0 %)\n"])

        def wait(self):
            self._dst.write_bytes(b"hw-encoded")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    assert result["encoder"] == "omxh264videoenc" and result["hw"] is True
    # A single GStreamer attempt: decoder 1/2 scale, no encoder scaler, no fallback.
    assert len(calls) == 1
    assert calls[0][0] == "gst-launch-1.0" and "omxh264videoenc" in calls[0]
    assert "scale=1" in calls[0]
    assert not any(str(t).startswith("output-height") for t in calls[0])
    assert (card / "Transcoded" / "clip_1080p-h264.mp4").read_bytes() == b"hw-encoded"


def test_process_cubie_two_stage_for_non_half_target(tmp_path, monkeypatch):
    # 4K -> 720p is not a 1/2 step: HW decodes to 1080p, then a CPU ffmpeg pass
    # finishes to 720p (the artifact-prone encoder scaler is never used).
    mgr, card = _cubie_mgr(tmp_path, monkeypatch)
    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._is_gst = cmd[0] == "gst-launch-1.0"
            self._dst = Path(cmd[-1].split("location=", 1)[-1]) if self._is_gst else Path(cmd[-1])
            self.returncode = None
            self.stdout = iter(["progressreport0 (00:00:05): 5 / 10 seconds (50.0 %)\n"]
                               if self._is_gst else [])

        def wait(self):
            self._dst.write_bytes(b"hw-1080" if self._is_gst else b"cpu-720")
            self.returncode = 0

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)

    job = mgr.submit("sdb1", "clip.mp4", "720p-h264")
    mgr._process(job["id"])

    result = mgr.snapshot()["jobs"][0]
    assert result["status"] == "done"
    # Two stages: hardware GStreamer (1/2 scale to 1080), then a CPU ffmpeg finish.
    assert len(calls) == 2
    assert calls[0][0] == "gst-launch-1.0" and "scale=1" in calls[0]
    assert calls[1][0] == "ffmpeg" and "libx264" in calls[1]
    assert result["encoder"] == "omxh264videoenc+cpu" and result["hw"] is True
    # The finished file is the CPU stage's output (no magenta bottom line).
    assert (card / "Transcoded" / "clip_720p-h264.mp4").read_bytes() == b"cpu-720"
    assert not (card / "Transcoded" / "clip_720p-h264.stage1.mp4").exists()  # cleaned up


def _cubie_mgr_with_perf(tmp_path, monkeypatch):
    mgr, card = _cubie_mgr(tmp_path, monkeypatch)   # 4K h264 -> 1080p = single-stage HW
    mgr._perf_file = str(tmp_path / "perf.json")
    mgr._perf = {}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": "h264", "width": 3840, "height": 2160, "fps": 59.94, "duration": 56.9,
        "has_audio": False, "acodec": None, "container": "mp4"})
    return mgr, card


def _run_cancel(mgr, monkeypatch, progress_line):
    """Submit a 1080p job whose (faked) encode emits ``progress_line`` then is
    canceled mid-run; run it to completion and return the finished job snapshot."""
    # Advance the monotonic clock on every read so the live estimate sees non-zero
    # elapsed wall time (the whole fake job otherwise runs within one clock tick).
    ticks = [1000.0]

    def _mono():
        ticks[0] += 1.0
        return ticks[0]

    monkeypatch.setattr(tc.time, "monotonic", _mono)
    holder = {}

    class _Proc:
        def __init__(self, cmd, **kw):
            self.returncode = None
            self.stdout = iter([progress_line])

        def wait(self):
            mgr._jobs[holder["id"]]["status"] = "canceled"  # cancel arrives mid-encode
            self.returncode = -15

        def terminate(self):  # pragma: no cover
            pass

    monkeypatch.setattr(tc.subprocess, "Popen", _Proc)
    monkeypatch.setattr(tc.subprocess, "run", lambda *a, **k: None)
    job = mgr.submit("sdb1", "clip.mp4", "1080p-h264")
    holder["id"] = job["id"]
    mgr._process(job["id"])
    return mgr.snapshot()["jobs"][0]


def test_cancel_keeps_a_stable_perf_sample(tmp_path, monkeypatch):
    mgr, _ = _cubie_mgr_with_perf(tmp_path, monkeypatch)
    # 12 s of output produced (>= the 10 s stability threshold) before the cancel.
    result = _run_cancel(mgr, monkeypatch,
                         "progressreport0 (00:00:20): 12 / 56 seconds (21.4 %)\n")
    assert result["status"] == "canceled"
    assert mgr._perf.get("h264:3840x2160:1080p-h264", {}).get("spf", 0) > 0  # learned
    # internal tracking fields never leak into the API
    assert "perf_spf" not in result and "perf_stable" not in result


def test_cancel_too_short_learns_nothing(tmp_path, monkeypatch):
    mgr, _ = _cubie_mgr_with_perf(tmp_path, monkeypatch)
    # only 2 s of output -> not stable -> the model is left untouched
    result = _run_cancel(mgr, monkeypatch,
                         "progressreport0 (00:00:03): 2 / 56 seconds (3.6 %)\n")
    assert result["status"] == "canceled"
    assert mgr._perf == {}


def test_cancel_still_learns_after_output_volume_unmounts(tmp_path, monkeypatch):
    # Regression: the cancel handler runs AFTER the `finally` unmounts the output
    # volume, so the source path is gone and a re-probe there fails. On the real
    # device this silently dropped every canceled sample. Model it: dropping the
    # rw mount makes the input file vanish, and a probe of a missing file returns
    # empty info (as ffprobe would). The fix captures the source info while still
    # mounted, so a long-enough canceled job still trains the estimate model.
    mgr, card = _cubie_mgr_with_perf(tmp_path, monkeypatch)
    good = tc.probe_video_info  # the good-info stub installed by _cubie_mgr_with_perf
    monkeypatch.setattr(tc, "probe_video_info", lambda src: good(src)
                        if Path(src).is_file()
                        else {"vcodec": None, "width": 0, "height": 0, "fps": 0.0,
                              "duration": None, "has_audio": False, "acodec": None,
                              "container": "mp4"})
    orig_umount = mgr._browse.umount_rw

    def _umount(dev):
        (card / "clip.mp4").unlink(missing_ok=True)  # the unmount hides the source
        return orig_umount(dev)

    monkeypatch.setattr(mgr._browse, "umount_rw", _umount)

    result = _run_cancel(mgr, monkeypatch,
                         "progressreport0 (00:00:20): 12 / 56 seconds (21.4 %)\n")
    assert result["status"] == "canceled"
    assert mgr._perf.get("h264:3840x2160:1080p-h264", {}).get("spf", 0) > 0


# --------------------------------------------------------------------------- #
# Planning + the performance/estimate model
# --------------------------------------------------------------------------- #

def test_perf_key_and_estimate_seconds():
    info = {"vcodec": "h264", "width": 3840, "height": 2160}
    assert perf_key(info, "1080p-h264") == "h264:3840x2160:1080p-h264"
    assert estimate_seconds(0.001, 60.0, 30.0) == pytest.approx(0.001 * 60 * 30)
    assert estimate_seconds(None, 60.0, 30.0) is None
    assert estimate_seconds(0.001, 0.0, 30.0) is None


def test_perf_model_learns_persists_and_scales_with_fps(tmp_path):
    mgr = _mgr(_FakeBrowse())
    mgr._perf_file = str(tmp_path / "perf.json")
    mgr._perf = {}
    # A resolution that is NOT one of the built-in seeds, so it starts empty.
    info = {"vcodec": "h264", "width": 2704, "height": 1520, "duration": 60.0, "fps": 30.0}
    assert mgr._estimate(info, "1080p-h264") is None            # no data yet
    mgr._update_perf(info, "1080p-h264", 90.0)                  # first sample
    assert (tmp_path / "perf.json").exists()
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(90.0)
    # double the framerate -> ~double the estimated time (frame-count based)
    assert mgr._estimate(dict(info, fps=60.0), "1080p-h264") == pytest.approx(180.0)
    # a small deviation is kept, a large one overwrites (spec: overwrite on notable)
    mgr._update_perf(info, "1080p-h264", 96.0)                  # +6.7% -> keep
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(90.0)
    mgr._update_perf(info, "1080p-h264", 150.0)                 # +66% -> overwrite
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(150.0)
    # a fresh manager reloads the persisted model
    mgr2 = _mgr(_FakeBrowse())
    mgr2._perf_file = str(tmp_path / "perf.json")
    mgr2._perf = mgr2._load_perf()
    assert mgr2._estimate(info, "1080p-h264") == pytest.approx(150.0)


def test_estimate_falls_back_to_seed_defaults(tmp_path):
    from copystation.transcode import DEFAULT_PERF

    mgr = _mgr(_FakeBrowse())
    mgr._perf_file = str(tmp_path / "perf.json")
    mgr._perf = {}  # fresh install: nothing learned yet
    mgr._board = "cubie"
    info = {"vcodec": "h264", "width": 3840, "height": 2160, "duration": 56.89, "fps": 59.94}
    # the built-in Cubie seed reproduces the measured ~85.5s for 4K60 -> 1080p
    seed = DEFAULT_PERF["cubie"]["h264:3840x2160:1080p-h264"]["spf"]
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(seed * 56.89 * 59.94)
    assert 80 < mgr._estimate(info, "1080p-h264") < 92
    # the seeds are hardware-specific: the Pi 4 uses its OWN seed, never the Cubie's
    mgr._board = "pi4"
    pi4_seed = DEFAULT_PERF["pi4"]["h264:3840x2160:1080p-h264"]["spf"]
    assert pi4_seed != seed  # different hardware -> different number
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(pi4_seed * 56.89 * 59.94)
    # a learned value always overrides the seed (any board)
    mgr._board = "cubie"
    mgr._perf["h264:3840x2160:1080p-h264"] = {"spf": 0.01}
    assert mgr._estimate(info, "1080p-h264") == pytest.approx(0.01 * 56.89 * 59.94)
    # an un-seeded (codec, resolution, preset) -> no estimate yet
    assert mgr._estimate(dict(info, width=1920, height=1080), "1080p-h264") is None


def test_plan_for_predicts_hw_hwcpu_and_cpu(tmp_path, monkeypatch):
    mgr, card = _cubie_mgr(tmp_path, monkeypatch)   # 4K h264 source on a Cubie
    hw = mgr.plan_for("sdb1", "clip.mp4", "1080p-h264")
    assert hw["path"] == "hw" and hw["out_height"] == 1080
    assert hw["info"]["width"] == 3840 and hw["info"]["size"] is not None
    two = mgr.plan_for("sdb1", "clip.mp4", "720p-h264")
    assert two["path"] == "hw+cpu" and two["out_height"] == 1080 and two["target_height"] == 720
    # a codec the hardware can't take -> the CPU path
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": "vp9", "width": 1920, "height": 1080, "fps": 30.0,
        "has_audio": False, "acodec": None, "container": "webm", "duration": 10.0})
    mgr._probe_cache.clear()
    cpu = mgr.plan_for("sdb1", "clip.mp4", "1080p-h264")
    assert cpu["path"] == "cpu"


# --------------------------------------------------------------------------- #
# Folder (batch) planning + submission
# --------------------------------------------------------------------------- #

def _folder_probe(src):
    """Vary the probed info by filename so a folder plan mixes HW and CPU paths."""
    name = Path(src).name.lower()
    if name.endswith((".mp4", ".mov")):  # 4K H.264 -> hardware-decodable
        return {"vcodec": "h264", "width": 3840, "height": 2160, "fps": 59.94,
                "duration": 10.0, "has_audio": False, "acodec": None,
                "container": name.rsplit(".", 1)[-1]}
    # a VP9 source the OMX pipeline can't decode -> CPU path
    return {"vcodec": "vp9", "width": 1920, "height": 1080, "fps": 30.0,
            "duration": 10.0, "has_audio": False, "acodec": None, "container": "mkv"}


def _folder_card(tmp_path):
    card = tmp_path / "card"
    dcim = card / "DCIM"
    dcim.mkdir(parents=True)
    for n in ("a.mp4", "b.mov", "d.mkv"):
        (dcim / n).write_bytes(b"\x00" * 32)
    (dcim / "c.txt").write_text("not a video")  # skipped
    return card


def _cubie_folder_mgr(tmp_path, monkeypatch):
    mgr = _mgr(_FakeBrowse({"sdb1": _folder_card(tmp_path)}))
    mgr._board = "cubie"
    mgr._encoders_avail = {"omxh264videoenc", "omxh264dec", "libx264"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_video_info", _folder_probe)
    return mgr


def test_is_video_file():
    assert tc.is_video_file("DJI_0001.MP4") and tc.is_video_file("clip.mkv")
    assert not tc.is_video_file("notes.txt") and not tc.is_video_file("cover.jpg")
    # .lrv proxies are intentionally excluded (low-res previews that collide with
    # the real clip's output name).
    assert not tc.is_video_file("DJI_0001.LRV")


def test_plan_folder_lists_videos_with_per_file_paths(tmp_path, monkeypatch):
    mgr = _cubie_folder_mgr(tmp_path, monkeypatch)
    plan = mgr.plan_folder("sdb1", "DCIM", "1080p-h264")
    # non-video (c.txt) is skipped; the rest are listed sorted.
    assert [f["name"] for f in plan["files"]] == ["a.mp4", "b.mov", "d.mkv"]
    assert plan["count"] == 3
    by = {f["name"]: f["plan"] for f in plan["files"]}
    assert by == {"a.mp4": "hw", "b.mov": "hw", "d.mkv": "cpu"}
    assert plan["counts"] == {"hw": 2, "hw+cpu": 0, "cpu": 1}
    # 720p splits the two 4K sources into a HW+CPU finish; VP9 stays on the CPU.
    plan720 = mgr.plan_folder("sdb1", "DCIM", "720p-h264")
    assert plan720["counts"] == {"hw": 0, "hw+cpu": 2, "cpu": 1}


def test_submit_folder_queues_one_job_per_video(tmp_path, monkeypatch):
    mgr = _cubie_folder_mgr(tmp_path, monkeypatch)
    result = mgr.submit_folder("sdb1", "DCIM", "720p-h264")
    assert result["count"] == 3
    assert sorted(j["input_path"] for j in result["jobs"]) == [
        "DCIM/a.mp4", "DCIM/b.mov", "DCIM/d.mkv"
    ]
    assert all(j["status"] == "queued" and j["preset"] == "720p-h264"
               for j in result["jobs"])
    # The batch owns the queue: a further single submit is refused as busy.
    assert mgr._has_active_job() is True
    with pytest.raises(TranscodeBusy):
        mgr.submit("sdb1", "DCIM/a.mp4", "720p-h264")


def test_submit_folder_no_videos_errors(tmp_path, monkeypatch):
    card = tmp_path / "card"
    (card / "empty").mkdir(parents=True)
    (card / "empty" / "notes.txt").write_text("x")
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    with pytest.raises(tc.TranscodeError):
        mgr.submit_folder("sdb1", "empty", "720p-h264")


def test_submit_folder_blocked_when_active(tmp_path, monkeypatch):
    mgr = _cubie_folder_mgr(tmp_path, monkeypatch)
    mgr.submit("sdb1", "DCIM/a.mp4", "720p-h264")  # one job already active
    with pytest.raises(TranscodeBusy):
        mgr.submit_folder("sdb1", "DCIM", "720p-h264")


# --------------------------------------------------------------------------- #
# Runtime settings (default preset + auto-transcode) + overlay persistence
# --------------------------------------------------------------------------- #

def _mgr_with_settings(tmp_path, overlay=None, cfg_overrides=None):
    """A manager whose user-settings overlay lives in ``tmp_path``.

    ``overlay`` is the *transcode-section* dict (wrapped into the on-disk
    ``{"transcode": {...}}`` layout) so callers only think about their keys.
    """
    import copy as _copy
    import json as _json

    from copystation.config import DEFAULTS

    data = _copy.deepcopy(DEFAULTS)
    data["user_settings_file"] = str(tmp_path / "user-settings.json")
    if cfg_overrides:
        data["transcode"].update(cfg_overrides)
    if overlay is not None:
        (tmp_path / "user-settings.json").write_text(_json.dumps({"transcode": overlay}))
    return TranscodeManager(Config(data), _hub(), _FakeBrowse())


def test_default_preset_falls_back_to_first_when_unset(tmp_path):
    mgr = _mgr_with_settings(tmp_path)
    assert mgr.default_preset == "1080p-h264"   # first configured preset
    assert mgr.auto_transcode is False


def test_invalid_default_preset_falls_back_to_first(tmp_path):
    mgr = _mgr_with_settings(tmp_path, cfg_overrides={"default_preset": "bogus"})
    assert mgr.default_preset == "1080p-h264"


def test_config_defaults_used_without_overlay(tmp_path):
    mgr = _mgr_with_settings(
        tmp_path, cfg_overrides={"default_preset": "720p-h264", "auto_transcode": True})
    assert mgr.default_preset == "720p-h264" and mgr.auto_transcode is True


def test_overlay_wins_over_config(tmp_path):
    mgr = _mgr_with_settings(
        tmp_path,
        overlay={"default_preset": "540p-h264", "auto_transcode": True},
        cfg_overrides={"default_preset": "720p-h264", "auto_transcode": False})
    assert mgr.default_preset == "540p-h264" and mgr.auto_transcode is True


def test_set_settings_persists_and_validates(tmp_path):
    import json as _json

    mgr = _mgr_with_settings(tmp_path)
    out = mgr.set_settings(default_preset="720p-h264", auto_transcode=True)
    assert out == {"default_preset": "720p-h264", "auto_transcode": True,
                   "output_location": "same"}
    # output_location was not touched, so only the two changed keys are persisted
    assert _json.loads((tmp_path / "user-settings.json").read_text()) == {
        "transcode": {"auto_transcode": True, "default_preset": "720p-h264"}}
    # a fresh manager over the same overlay reloads the persisted values
    mgr2 = TranscodeManager(mgr._config, _hub(), _FakeBrowse())
    assert mgr2.default_preset == "720p-h264" and mgr2.auto_transcode is True
    # only the provided field changes
    mgr.set_settings(auto_transcode=False)
    assert mgr.default_preset == "720p-h264" and mgr.auto_transcode is False
    # an unknown preset is rejected
    with pytest.raises(UnknownPreset):
        mgr.set_settings(default_preset="nope")
    # the output location persists, reloads (overlay wins) and is validated
    assert mgr.set_settings(output_location="central")["output_location"] == "central"
    assert _json.loads((tmp_path / "user-settings.json").read_text())[
        "transcode"]["output_location"] == "central"
    assert TranscodeManager(mgr._config, _hub(), _FakeBrowse()).output_location == "central"
    with pytest.raises(InvalidSetting):
        mgr.set_settings(output_location="nowhere")


def test_snapshot_exposes_settings_and_queue(tmp_path):
    mgr = _mgr_with_settings(tmp_path)
    snap = mgr.snapshot()
    assert snap["default_preset"] == "1080p-h264"
    assert snap["auto_transcode"] is False
    assert snap["output_location"] == "same"
    assert snap["queue"] == {"pending": 0, "index": 0, "count": 0,
                             "percent": 0.0, "elapsed_seconds": None, "eta_seconds": None}


def test_transcode_overlay_prunes_stale_keys(tmp_path):
    # A settings file left by another version with a since-removed key must load
    # robustly: the unknown key is dropped from the overlay (never config.yaml).
    import json as _json

    (tmp_path / "user-settings.json").write_text(_json.dumps(
        {"transcode": {"auto_transcode": True, "default_preset": "540p-h264",
                       "legacy_key": "x"}}))
    mgr = _mgr_with_settings(tmp_path)
    assert mgr.auto_transcode is True and mgr.default_preset == "540p-h264"
    assert _json.loads((tmp_path / "user-settings.json").read_text()) == {
        "transcode": {"auto_transcode": True, "default_preset": "540p-h264"}}


def test_auto_transcode_mirrored_onto_shared_state(tmp_path):
    # The manager mirrors the auto-transcode flag onto StationState (for the
    # e-paper badge / web header) at construction and whenever it changes.
    hub = _hub()
    import copy as _copy

    from copystation.config import DEFAULTS
    data = _copy.deepcopy(DEFAULTS)
    data["user_settings_file"] = str(tmp_path / "user-settings.json")
    data["transcode"]["auto_transcode"] = True
    mgr = TranscodeManager(Config(data), hub, _FakeBrowse())
    assert hub.state.snapshot()["auto_transcode"] is True   # set at init
    mgr.set_settings(auto_transcode=False)
    assert hub.state.snapshot()["auto_transcode"] is False  # updated on change


# --------------------------------------------------------------------------- #
# Auto-transcode submission + queue aggregate + the batch worker
# --------------------------------------------------------------------------- #

def test_submit_auto_queues_jobs_with_estimates(tmp_path, monkeypatch):
    mgr, card = _cubie_mgr_with_perf(tmp_path, monkeypatch)  # seeds a 1080p estimate
    (card / "b.mp4").write_bytes(b"\x00" * 32)
    res = mgr.submit_auto("sdb1", ["clip.mp4", "b.mp4"], mount_root=card,
                          preset_id="1080p-h264")
    assert res == {"count": 2, "preset": "1080p-h264",
                   "jobs": res["jobs"]} and len(res["jobs"]) == 2
    jobs = {j["input_path"]: j for j in mgr.snapshot()["jobs"]}
    assert set(jobs) == {"clip.mp4", "b.mp4"}
    assert all(j["output_device"] == "sdb1" and j["preset"] == "1080p-h264"
               for j in jobs.values())
    # the perf seed produced a positive per-job estimate
    assert all(j["estimate_seconds"] and j["estimate_seconds"] > 0
               for j in jobs.values())


def test_submit_auto_uses_default_preset_when_unset(tmp_path, monkeypatch):
    from copystation.settings_store import SettingsStore

    mgr, card = _cubie_mgr_with_perf(tmp_path, monkeypatch)
    mgr._settings = SettingsStore(str(tmp_path / "user-settings.json")).section("transcode")
    mgr.set_settings(default_preset="1080p-h264")
    res = mgr.submit_auto("sdb1", ["clip.mp4"], mount_root=card)  # no preset_id
    assert res["preset"] == "1080p-h264"


def test_queue_aggregate_counts_pending_and_sums_estimates(tmp_path, monkeypatch):
    mgr, card = _cubie_mgr_with_perf(tmp_path, monkeypatch)
    (card / "b.mp4").write_bytes(b"\x00" * 32)
    mgr.submit_auto("sdb1", ["clip.mp4", "b.mp4"], mount_root=card,
                    preset_id="1080p-h264")
    q = mgr.snapshot()["queue"]
    assert q["pending"] == 2 and q["count"] == 2 and q["index"] == 1
    assert q["percent"] == 0.0
    assert q["eta_seconds"] and q["eta_seconds"] > 0   # sum of both estimates


def test_queue_eta_extrapolates_when_queued_estimates_missing(tmp_path, monkeypatch):
    # A batch whose source key is neither seeded nor learned: the queued jobs have
    # no estimate of their own, so the running job's live projection stands in for
    # them -- the total reflects ALL pending jobs, not just the running one.
    card = _card(tmp_path)
    (card / "b.mp4").write_bytes(b"\x00" * 32)
    (card / "c.mp4").write_bytes(b"\x00" * 32)
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    # No mount_root -> submit_auto records no per-job estimates.
    mgr.submit_auto("sdb1", ["clip.mp4", "b.mp4", "c.mp4"], preset_id="720p-h264")
    ids = list(mgr._order)
    assert all(mgr._jobs[i]["estimate_seconds"] is None for i in ids)
    # First job running, 50% done after 20s of wall time -> ~20s remaining and a
    # projected full time of ~40s used as the fallback for the two queued jobs.
    mgr._set(ids[0], status="running", started=tc.time.monotonic() - 20.0, percent=50)
    q = mgr.snapshot()["queue"]
    assert q["pending"] == 3 and q["index"] == 1
    # ~20 (running) + ~40 + ~40 (two extrapolated) -> well above the running job alone
    assert q["eta_seconds"] is not None and q["eta_seconds"] > 60


def test_queue_elapsed_spans_the_whole_run(tmp_path, monkeypatch):
    # The queue's elapsed time is the whole run's wall time, not just the current
    # file's -- so it keeps climbing across files instead of resetting each time.
    card = _card(tmp_path)
    (card / "b.mp4").write_bytes(b"\x00" * 32)
    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    mgr.submit_auto("sdb1", ["clip.mp4", "b.mp4"], preset_id="720p-h264")
    ids = list(mgr._order)
    # Run started 30s ago; the current (first) file only started 5s ago.
    mgr._run_started = tc.time.monotonic() - 30.0
    mgr._set(ids[0], status="running", started=tc.time.monotonic() - 5.0, percent=40)
    q = mgr.snapshot()["queue"]
    assert q["elapsed_seconds"] is not None and q["elapsed_seconds"] >= 29


def test_run_batch_processes_all_jobs_and_restores_phase_once(tmp_path, monkeypatch):
    card = _card(tmp_path)
    (card / "b.mp4").write_bytes(b"\x00" * 32)
    hub = _hub()
    hub.state.set_phase(State.DETECTING)   # a card was detected before the batch
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

    mgr.submit_auto("sdb1", ["clip.mp4", "b.mp4"], mount_root=card,
                    preset_id="720p-h264")
    first = mgr._queue.get_nowait()   # the worker pulls the first job, then _run_batch
    mgr._run_batch(first)

    statuses = {j["input_path"]: j["status"] for j in mgr.snapshot()["jobs"]}
    assert statuses == {"clip.mp4": "done", "b.mp4": "done"}
    assert (card / "Transcoded" / "clip_720p-h264.mp4").exists()
    assert (card / "Transcoded" / "b_720p-h264.mp4").exists()
    # The phase is restored ONCE, after the whole batch (not flashed per file).
    assert hub.state.phase is State.DETECTING
    assert hub.state.snapshot()["transcode"]["active"] is False


def test_process_cubie_falls_back_to_cpu_for_unsupported_source(tmp_path, monkeypatch):
    card = _card(tmp_path)
    browse = _FakeBrowse({"sdb1": card})
    mgr = _mgr(browse)
    mgr._board = "cubie"
    mgr._encoders_avail = {"omxh264videoenc", "omxh264dec", "libx264"}
    monkeypatch.setattr(mgr, "_ensure_worker", lambda: None)
    monkeypatch.setattr(tc, "probe_duration", lambda src: 10.0)
    # A container/codec the OMX pipeline can't take (VP9) -> must skip to the CPU.
    monkeypatch.setattr(tc, "probe_video_info", lambda src: {
        "vcodec": "vp9", "width": 3840, "height": 2160,
        "has_audio": False, "acodec": None, "container": "webm"})

    calls = []

    class _Proc:
        def __init__(self, cmd, **kw):
            calls.append(cmd)
            self._dst = Path(cmd[-1])
            self.returncode = None
            self.stdout = iter([])

        def wait(self):
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
    # The GStreamer candidate was skipped up front (no wasted encode); CPU ran once.
    assert result["encoder"] == "cpu" and result["hw"] is False
    assert len(calls) == 1 and calls[0][0] == "ffmpeg"


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


def test_ram_budget():
    assert ram_budget(3000, 2 / 3) == 2000
    assert ram_budget(0, 0.5) == 0


def test_parse_bitrate():
    assert parse_bitrate("8M") == 8_000_000
    assert parse_bitrate("2500k") == 2_500_000
    assert parse_bitrate(8_000_000) == 8_000_000
    assert parse_bitrate("") == 0
    assert parse_bitrate("bogus") == 0


def test_estimate_output_bytes_uses_bitrate_and_duration():
    # 8 Mbit/s video + 128 kbit/s audio over 60 s, x1.5 headroom.
    est = estimate_output_bytes(60.0, {"height": 1080, "bitrate": "8M"})
    approx = (8_000_000 + 128_000) / 8 * 60 * 1.5
    assert abs(est - approx) < 2
    # unknown duration -> 0 (stream to the card, do not buffer)
    assert estimate_output_bytes(None, {"height": 720}) == 0
    assert estimate_output_bytes(0, {"height": 720}) == 0


def test_encode_buffers_output_in_ram_when_it_fits(tmp_path, monkeypatch):
    import shutil as _sh

    card = tmp_path / "card"
    card.mkdir()
    src = card / "clip.mp4"
    src.write_bytes(b"input" * 10)
    out_dir = card / "Transcoded"
    out_dir.mkdir()
    final_dst = out_dir / "clip_720p.mp4"

    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(tc, "probe_duration", lambda s: 60.0)   # known duration
    monkeypatch.setattr(mgr, "_ram_budget", lambda: 10 ** 9)     # plenty
    monkeypatch.setattr(mgr, "_work_base", str(tmp_path / "work"))
    monkeypatch.setattr(mgr, "_mount_tmpfs", lambda path, size: path.mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(mgr, "_umount_tmpfs", lambda path: _sh.rmtree(path, ignore_errors=True))

    seen = {}

    def fake_encode(job_id, s, d, preset, duration=None):
        seen["src"] = Path(s)
        seen["dst"] = Path(d)
        Path(d).write_bytes(b"encoded")

    monkeypatch.setattr(mgr, "_encode_with_fallback", fake_encode)

    mgr._encode(1, src, final_dst, {"id": "720p-h264", "height": 720})

    # Input streams straight from the card; only the OUTPUT is in the RAM work dir,
    # then copied back to the card.
    assert seen["src"] == src
    assert str(tmp_path / "work") in str(seen["dst"])
    assert final_dst.read_bytes() == b"encoded"


def test_encode_streams_to_card_when_output_too_large(tmp_path, monkeypatch):
    card = tmp_path / "card"
    card.mkdir()
    src = card / "clip.mp4"
    src.write_bytes(b"x" * 1000)
    out_dir = card / "Transcoded"
    out_dir.mkdir()
    final_dst = out_dir / "out.mp4"

    mgr = _mgr(_FakeBrowse({"sdb1": card}))
    monkeypatch.setattr(tc, "probe_duration", lambda s: 3600.0)  # long -> big output
    monkeypatch.setattr(mgr, "_ram_budget", lambda: 1000)        # tiny budget
    monkeypatch.setattr(mgr, "_mount_tmpfs",
                        lambda *a: (_ for _ in ()).throw(AssertionError("must not mount")))

    seen = {}

    def fake_encode(job_id, s, d, preset, duration=None):
        seen["src"] = Path(s)
        Path(d).write_bytes(b"e")

    monkeypatch.setattr(mgr, "_encode_with_fallback", fake_encode)

    mgr._encode(1, src, final_dst, {"id": "720p-h264", "height": 720})
    assert seen["src"] == src           # encoded straight from the card
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
            "output_location": "same",
            "default_preset": "720p-h264",
            "auto_transcode": False,
            "presets": [{"id": "720p-h264", "label": "720p H.264"}],
            "queue": {"pending": 0, "index": 0, "count": 0,
                      "percent": 0.0, "eta_seconds": None},
            "jobs": self.jobs,
        }

    def set_settings(self, default_preset=None, auto_transcode=None,
                     output_location=None):
        if default_preset == "bad":
            raise UnknownPreset("bad")
        if output_location == "bad":
            raise InvalidSetting("bad")
        return {"default_preset": default_preset or "720p-h264",
                "auto_transcode": bool(auto_transcode),
                "output_location": output_location or "same"}

    def submit(self, device, path, preset):
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

    def plan_folder(self, device, path, preset):
        if not self._available:
            raise TranscodeUnavailable("no ffmpeg")
        if preset == "bad":
            raise UnknownPreset("bad")
        if device == "sdX":
            raise UnknownVolume("sdX")
        return {
            "preset": preset, "folder": path, "count": 2,
            "counts": {"hw": 1, "hw+cpu": 0, "cpu": 1},
            "files": [{"name": "a.mp4", "plan": "hw"}, {"name": "b.mkv", "plan": "cpu"}],
            "estimate_seconds": 20.0,
        }

    def submit_folder(self, device, path, preset):
        if not self._available:
            raise TranscodeUnavailable("no ffmpeg")
        if preset == "busy":
            raise TranscodeBusy("a copy is in progress")
        if preset == "empty":
            raise tc.TranscodeError("no video files in this folder")
        if device == "sdX":
            raise UnknownVolume("sdX")
        self.jobs = [{"id": 7, "status": "queued", "input_path": f"{path}/a.mp4"},
                     {"id": 8, "status": "queued", "input_path": f"{path}/b.mkv"}]
        return {"jobs": self.jobs, "count": 2}


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


def test_api_folder_plan_and_submit():
    client = _client(_FakeManager())
    p = client.get("/api/transcode/folder-plan",
                   params={"device": "sdb1", "path": "DCIM", "preset": "720p-h264"})
    assert p.status_code == 200
    body = p.json()
    assert body["count"] == 2 and body["counts"]["hw"] == 1
    assert [f["plan"] for f in body["files"]] == ["hw", "cpu"]

    r = client.post("/api/transcode/folder",
                    json={"device": "sdb1", "path": "DCIM", "preset": "720p-h264"})
    assert r.status_code == 200
    assert r.json()["count"] == 2 and len(r.json()["jobs"]) == 2


def test_api_folder_errors():
    client = _client(_FakeManager())
    assert client.get("/api/transcode/folder-plan",
                      params={"device": "sdX", "path": "D", "preset": "720p-h264"}).status_code == 404
    assert client.get("/api/transcode/folder-plan",
                      params={"device": "sdb1", "path": "D", "preset": "bad"}).status_code == 400
    assert client.post("/api/transcode/folder",
                       json={"device": "sdb1", "path": "D", "preset": "busy"}).status_code == 409
    # a folder with no video files -> 400 (TranscodeError)
    assert client.post("/api/transcode/folder",
                       json={"device": "sdb1", "path": "D", "preset": "empty"}).status_code == 400


def test_api_folder_unavailable_is_501():
    client = _client(_FakeManager(available=False))
    assert client.post("/api/transcode/folder",
                       json={"device": "sdb1", "path": "D", "preset": "720p-h264"}).status_code == 501


def test_api_transcode_snapshot_exposes_settings_and_queue():
    client = _client(_FakeManager())
    body = client.get("/api/transcode").json()
    assert body["default_preset"] == "720p-h264"
    assert body["auto_transcode"] is False
    assert body["queue"]["pending"] == 0


def test_api_transcode_settings_persists():
    client = _client(_FakeManager())
    r = client.post("/api/transcode/settings",
                    json={"default_preset": "720p-h264", "auto_transcode": True})
    assert r.status_code == 200
    assert r.json() == {"default_preset": "720p-h264", "auto_transcode": True,
                        "output_location": "same"}


def test_api_transcode_settings_bad_preset_is_400():
    client = _client(_FakeManager())
    r = client.post("/api/transcode/settings", json={"default_preset": "bad"})
    assert r.status_code == 400


def test_api_transcode_settings_bad_location_is_400():
    client = _client(_FakeManager())
    r = client.post("/api/transcode/settings", json={"output_location": "bad"})
    assert r.status_code == 400


def test_api_transcode_settings_allowed_during_copy():
    # The switch/preset must be changeable mid-copy (evaluated only when the copy
    # finishes), so the settings endpoint is NOT gated on the COPYING phase.
    state = StationState()
    state.set_phase(State.COPYING)
    client = TestClient(create_app(state, Config(), transcode=_FakeManager()))
    r = client.post("/api/transcode/settings", json={"auto_transcode": True})
    assert r.status_code == 200


def test_transcode_routes_absent_without_manager():
    client = TestClient(create_app(StationState(), Config(), transcode=None))
    assert client.get("/api/transcode").status_code == 404
    assert client.get("/api/settings").json()["features"]["transcode"] is False


class _FakeDeleteBrowse:
    """Minimal browse manager for the delete endpoint tests."""

    def __init__(self, allow_delete=True):
        self.allow_delete = allow_delete
        self.deleted = []

    def list_volumes(self):
        return []

    def delete_file(self, device, path):
        if not self.allow_delete:
            raise PathEscapesVolume("deletes are disabled")
        if path == "missing":
            raise NotFound("nope")
        self.deleted.append((device, path))


def test_api_delete_file_reports_feature_and_serialises():
    browse = _FakeDeleteBrowse()
    state = StationState()
    client = TestClient(create_app(state, Config(), browse=browse, transcode=None))
    assert client.get("/api/settings").json()["features"]["delete"] is True

    res = client.delete("/api/files?device=sdb1&path=DCIM/x.mp4")
    assert res.status_code == 200 and browse.deleted == [("sdb1", "DCIM/x.mp4")]
    # a missing file -> 404 from the mapped BrowseError
    assert client.delete("/api/files?device=sdb1&path=missing").status_code == 404
    # refused while a copy is in progress
    state.set_phase(State.COPYING)
    assert client.delete("/api/files?device=sdb1&path=DCIM/x.mp4").status_code == 409


def test_api_delete_disabled_feature_flag():
    client = TestClient(create_app(StationState(), Config(),
                                   browse=_FakeDeleteBrowse(allow_delete=False)))
    assert client.get("/api/settings").json()["features"]["delete"] is False
