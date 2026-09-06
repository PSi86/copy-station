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


def test_index_links_the_project_with_an_inline_icon():
    """The page footer carries the GitHub link, spelled out, with an inline mark.

    Both properties matter for the same reason: a client reading this is usually
    joined to the station's own access point and has no internet. It cannot follow
    the link there and then -- so the repository name has to be written out rather
    than reduced to an icon -- and an icon fetched from a CDN would not load at
    all, so the SVG has to live in the document.
    """
    res = _client(StationState()).get("/")
    assert 'href="https://github.com/PSi86/copy-station"' in res.text
    assert "PSi86/copy-station</span>" in res.text
    assert '<svg class="repo-icon"' in res.text


def test_settings_placeholder():
    res = _client(StationState()).get("/api/settings")
    assert res.status_code == 200
    assert res.json()["editable"] is False


def test_ap_status_in_status_endpoint():
    state = StationState()
    assert _client(state).get("/api/status").json()["wifi_ap"] is False
    state.set_ap_active(True)
    assert _client(state).get("/api/status").json()["wifi_ap"] is True


class _FakeApControl:
    """Stand-in for wifi_ap.ApController (no nmcli in the test suite)."""

    def __init__(self, bring_up_works=True):
        self.bring_up_works = bring_up_works
        self.calls = []
        self.active = False

    def apply(self, active):
        self.calls.append(active)
        self.active = bool(active) and self.bring_up_works
        return self.active


def test_wifi_ap_switch_absent_without_a_controller():
    # No AP configured -> no endpoint and no switch in the frontend.
    client = _client(StationState())
    assert client.get("/api/settings").json()["features"]["wifi_ap"] is False
    assert client.post("/api/wifi_ap", json={"enabled": True}).status_code == 404


def test_wifi_ap_switch_turns_the_ap_on_and_off():
    control = _FakeApControl()
    client = TestClient(create_app(StationState(), wifi_ap=control))
    assert client.get("/api/settings").json()["features"]["wifi_ap"] is True

    res = client.post("/api/wifi_ap", json={"enabled": True})
    assert res.status_code == 200 and res.json() == {"enabled": True}
    res = client.post("/api/wifi_ap", json={"enabled": False})
    assert res.status_code == 200 and res.json() == {"enabled": False}
    assert control.calls == [True, False]


def test_wifi_ap_switch_reports_a_failed_bringup():
    control = _FakeApControl(bring_up_works=False)
    client = TestClient(create_app(StationState(), wifi_ap=control))
    res = client.post("/api/wifi_ap", json={"enabled": True})
    assert res.status_code == 503
    assert "password" in res.json()["detail"]


def test_address_availability_probe():
    from copystation.web import address_available, is_wildcard_host

    for wildcard in ("0.0.0.0", "", "::", "*"):
        assert is_wildcard_host(wildcard) is True
    assert is_wildcard_host("192.168.1.50") is False
    assert address_available("127.0.0.1") is True
    assert address_available("10.255.255.1") is False  # not an address of this box


def _free_port():
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_web_server_does_not_block_on_a_missing_host_and_binds_later(monkeypatch):
    """The boot race: web.host appears late, the station must not wait for it."""
    import threading
    import time
    from http.client import HTTPConnection

    import copystation.web as web

    # The address is "not up yet" until the event is set (an interface coming up).
    appeared = threading.Event()
    monkeypatch.setattr(web, "address_available", lambda host: appeared.is_set())
    monkeypatch.setattr(web, "_BIND_RETRY_START", 0.02)
    monkeypatch.setattr(web, "_BIND_RETRY_MAX", 0.02)

    port = _free_port()
    began = time.monotonic()
    thread = web.start_web_server(StationState(), "127.0.0.1", port)
    # Returned at once instead of waiting for the address: the daemon walks on to
    # the device watcher and the station copies regardless of the network.
    assert time.monotonic() - began < 1.0
    assert thread.is_alive()

    appeared.set()  # the interface comes up
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            conn = HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", "/api/status")
            assert conn.getresponse().status == 200
            conn.close()
            return  # bound and serving without any restart
        except OSError:
            time.sleep(0.05)
    raise AssertionError("the web server never bound after the address appeared")


def test_web_server_still_raises_on_a_real_bind_failure():
    """A taken port is a config error, not a wait -- it must be reported at once."""
    import socket

    import copystation.web as web

    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        with pytest.raises(RuntimeError, match="could not bind"):
            web.start_web_server(StationState(), "127.0.0.1", port)


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
