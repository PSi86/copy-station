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
