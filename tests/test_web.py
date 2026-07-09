import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from copystation.state import StationState, StorageInfo  # noqa: E402
from copystation.web.app import create_app  # noqa: E402


def _client(state):
    return TestClient(create_app(state))


def test_status_endpoint_shape():
    state = StationState()
    state.begin_transfer("transfer_0001_DJI_O4", 2000)
    state.update_progress(1000)
    state.set_storage(
        StorageInfo(label="DJI_O4", capacity=23_000_000_000, used=10_000_000_000, free=13_000_000_000),
        StorageInfo(label="target", capacity=256_000_000_000, used=1_000_000, free=255_999_000_000),
    )

    res = _client(state).get("/api/status")
    assert res.status_code == 200
    data = res.json()

    assert data["phase"] == "copying"
    assert data["percent"] == 50.0
    assert data["bytes_total"] == 2000
    assert data["transfer_name"] == "transfer_0001_DJI_O4"
    assert data["source"]["label"] == "DJI_O4"
    assert data["target"]["capacity"] == 256_000_000_000
    for key in ("elapsed_seconds", "eta_seconds", "source", "target"):
        assert key in data


def test_index_served():
    res = _client(StationState()).get("/")
    assert res.status_code == 200
    assert "Copy_Station" in res.text


def test_settings_placeholder():
    res = _client(StationState()).get("/api/settings")
    assert res.status_code == 200
    assert res.json()["editable"] is False


def test_ap_status_in_status_endpoint():
    state = StationState()
    assert _client(state).get("/api/status").json()["wifi_ap"] is False
    state.set_ap_active(True)
    assert _client(state).get("/api/status").json()["wifi_ap"] is True


def test_transcode_phase_and_block_in_snapshot():
    state = StationState()
    assert state.snapshot()["transcode"] == {"active": False}

    state.begin_transcode("DJI_0219.MP4")
    snap = state.snapshot()
    assert snap["phase"] == "transcoding"  # overrides the copy phase
    tr = snap["transcode"]
    assert tr["active"] is True and tr["name"] == "DJI_0219.MP4"

    state.update_transcode(0.5, "cpu", False)
    state.set_transcode_meta(input_size=210_000_000, fps=25.0)
    tr = state.snapshot()["transcode"]
    assert tr["percent"] == 50.0
    assert tr["encoder"] == "cpu"
    assert tr["elapsed_seconds"] is not None
    assert tr["input_size"] == 210_000_000
    assert tr["fps"] == 25.0

    state.finish_transcode()
    assert state.snapshot()["transcode"] == {"active": False}
