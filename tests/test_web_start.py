"""Start-up behaviour of the web server thread (bind success/failure)."""

import threading

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

import uvicorn  # noqa: E402

from copystation import web  # noqa: E402
from copystation.state import StationState  # noqa: E402

# TEST-NET-3: never assigned to a host, so the bind fails with EADDRNOTAVAIL
# (errno 99) -- exactly the tester's "cannot assign requested address".
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


def test_failed_bind_raises_instead_of_reporting_success():
    with pytest.raises(RuntimeError) as excinfo:
        web.start_web_server(StationState(), UNASSIGNED_HOST, 8080)
    message = str(excinfo.value)
    assert f"{UNASSIGNED_HOST}:8080" in message
    assert "0.0.0.0" in message  # points at the fix


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
