"""Start-up behaviour of the web server thread (bind success/failure/pending)."""

import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import uvicorn  # noqa: E402

from copystation import web  # noqa: E402
from copystation.state import StationState  # noqa: E402

# TEST-NET-3: never assigned to a host, so a bind would fail with EADDRNOTAVAIL
# (errno 99) -- "cannot assign requested address". Indistinguishable from an
# address whose interface is merely not up YET, which is why it is waited for.
UNASSIGNED_HOST = "203.0.113.1"


class _FakeServer:
    """uvicorn.Server stand-in: reports `started` without touching a socket."""

    def __init__(self, config, start: bool = True) -> None:
        self.config = config
        self.started = False
        self.install_signal_handlers = True
        self._start = start
        self._stop = threading.Event()

    def run(self) -> None:
        if self._start:
            self.started = True
        self._stop.wait(10)


def test_missing_host_address_is_waited_for_not_reported_as_success(monkeypatch, caplog):
    # A host address that is not on this machine is NOT an error: at boot it is
    # usually just DHCP being slower than this service. The daemon is released
    # immediately (the station copies without a network) and the server thread
    # binds whenever the address turns up.
    monkeypatch.setattr(web, "_BIND_RETRY_START", 5.0)  # keep the idle retry quiet
    monkeypatch.setattr(web, "_BIND_RETRY_MAX", 5.0)
    with caplog.at_level("INFO", logger="copystation.web"):
        thread = web.start_web_server(StationState(), UNASSIGNED_HOST, 8080)
    assert thread.is_alive()
    assert UNASSIGNED_HOST in caplog.text
    assert "0.0.0.0" in caplog.text  # points at the fix
    # ... and it must never claim to be serving in the meantime.
    assert "listening on" not in caplog.text


def test_successful_start_is_logged_once_listening(monkeypatch, caplog):
    monkeypatch.setattr(uvicorn, "Server", lambda config: _FakeServer(config))
    with caplog.at_level("INFO", logger="copystation.web"):
        thread = web.start_web_server(StationState(), "127.0.0.1", 8099)
    assert thread.is_alive()
    assert "listening on http://127.0.0.1:8099" in caplog.text


def test_server_that_never_confirms_only_warns(monkeypatch, caplog):
    monkeypatch.setattr(uvicorn, "Server", lambda config: _FakeServer(config, start=False))
    monkeypatch.setattr(web, "_STARTUP_TIMEOUT", 0.2)
    with caplog.at_level("WARNING", logger="copystation.web"):
        thread = web.start_web_server(StationState(), "127.0.0.1", 8099)
    assert thread.is_alive()  # the daemon carries on regardless
    assert "did not confirm" in caplog.text
