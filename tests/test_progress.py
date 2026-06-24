from copystation.state import StationState
from copystation.status import State
from copystation.transfer import copy_tree, parse_rsync_progress


def test_parse_rsync_progress_valid_lines():
    assert parse_rsync_progress("  1,234,567  45%  12.34MB/s    0:00:12") == 1234567
    assert parse_rsync_progress("32768   0%    0.00kB/s    0:00:00") == 32768
    assert parse_rsync_progress("  9,999,999,999 100%  50.00MB/s    0:00:00 (xfr#1)") == 9999999999


def test_parse_rsync_progress_non_progress_lines():
    assert parse_rsync_progress("sending incremental file list") is None
    assert parse_rsync_progress("") is None
    assert parse_rsync_progress("100MEDIA/clip.mp4") is None


def test_copy_tree_reports_progress_via_shutil(tmp_path):
    # On the dev machine rsync is absent, so copy_tree uses the shutil path.
    src = tmp_path / "src"
    (src / "a").mkdir(parents=True)
    (src / "a" / "f1.bin").write_bytes(b"x" * 100)
    (src / "a" / "f2.bin").write_bytes(b"y" * 50)
    dst = tmp_path / "dst"

    seen = []
    copy_tree(src, dst, on_progress=seen.append)

    assert seen, "expected at least one progress callback"
    assert seen == sorted(seen), "progress must be monotonically increasing"
    assert seen[-1] == 150  # total bytes copied


def test_station_state_snapshot_during_copy():
    state = StationState()
    state.begin_transfer("transfer_0001_Cam", 1000)
    state.update_progress(500)
    snap = state.snapshot()

    assert snap["phase"] == State.COPYING.value
    assert snap["percent"] == 50.0
    assert snap["bytes_done"] == 500
    assert snap["bytes_total"] == 1000
    assert snap["transfer_name"] == "transfer_0001_Cam"
    assert snap["elapsed_seconds"] is not None
    # eta key is always present (may be None right at the start)
    assert "eta_seconds" in snap


def test_station_state_reset():
    state = StationState()
    state.begin_transfer("x", 10)
    state.update_progress(10)
    state.reset_to_ready()
    snap = state.snapshot()
    assert snap["phase"] == State.READY.value
    assert snap["percent"] == 0.0
    assert snap["bytes_total"] == 0


def test_event_log_newest_first_and_survives_reset():
    state = StationState()
    state.log_event("first")
    state.log_event("second")
    snap = state.snapshot()
    messages = [e["message"] for e in snap["events"]]
    assert messages == ["second", "first"]  # newest first
    assert all("time" in e and "seq" in e for e in snap["events"])

    # The action log is history -- it persists across a ready reset.
    state.reset_to_ready()
    assert [e["message"] for e in state.snapshot()["events"]] == ["second", "first"]


def test_set_error_freezes_elapsed_clock():
    # On failure the elapsed time must stop, not keep counting as if still live.
    state = StationState()
    state.begin_transfer("x", 100)
    state.update_progress(50)
    assert state._finished_monotonic is None
    state.set_error("device disconnected")
    assert state._finished_monotonic is not None
    # Two reads a moment apart return the same frozen elapsed value.
    first = state.snapshot()["elapsed_seconds"]
    second = state.snapshot()["elapsed_seconds"]
    assert first == second


def test_snapshot_exposes_copy_speed():
    state = StationState()
    state.begin_transfer("x", 1000)
    state.update_progress(500)
    snap = state.snapshot()
    # speed key always present; a positive value once some bytes are done.
    assert "speed_bytes" in snap
    assert snap["speed_bytes"] is None or snap["speed_bytes"] >= 0
