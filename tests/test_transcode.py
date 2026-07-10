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
    # the seeds are hardware-specific: a Pi must NOT use the Cubie's numbers
    mgr._board = "pi4"
    assert mgr._estimate(info, "1080p-h264") is None
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
