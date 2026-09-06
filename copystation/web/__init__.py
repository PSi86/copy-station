"""Optional local web interface (FastAPI).

Started in a background daemon thread by the main daemon when ``web.enabled`` is
set. Binds to ``0.0.0.0`` by default so it serves every network interface,
including ones that come up or go down at runtime (the WiFi AP!) -- a wildcard
listening socket needs no per-interface rebinding. A configured concrete
``web.host`` is honoured, but then only that one address is served.

The network is treated as something that *arrives*, not as a precondition: a
concrete ``web.host`` that does not exist yet (the service can easily win the
race against DHCP at boot) is waited for **in the background**, so the copy
functionality -- the actual job of this station -- is available immediately and
never waits for a network it does not need.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..config import Config
    from ..state import StationState

_LOG = logging.getLogger("copystation.web")

# How long to wait for uvicorn to confirm it is listening before giving up on a
# verdict. A bind either succeeds or fails within milliseconds; the margin only
# covers a slow board under boot load.
_STARTUP_TIMEOUT = 5.0

# Retry cadence while waiting for a CONCRETE web.host to appear: quick at first
# (a DHCP lease usually lands within seconds of boot), then backing off so a host
# that never appears costs nothing but a log line every now and then.
_BIND_RETRY_START = 1.0
_BIND_RETRY_MAX = 10.0
_BIND_WAIT_LOG_INTERVAL = 60.0

# Hosts that bind every interface -- nothing to wait for.
_WILDCARD_HOSTS = ("", "0.0.0.0", "::", "*")


def is_wildcard_host(host: str) -> bool:
    """Whether ``host`` binds every interface (present and future)."""
    return str(host or "").strip() in _WILDCARD_HOSTS


def address_available(host: str) -> bool:
    """Whether ``host`` is an address this machine can currently bind."""
    try:
        infos = socket.getaddrinfo(host, 0, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, socktype, proto, _canonname, sockaddr in infos:
        try:
            with socket.socket(family, socktype, proto) as probe:
                probe.bind(sockaddr)  # port 0: never collides with the real bind
            return True
        except OSError:
            continue
    return False


def _wait_until_bindable(host: str, pending: threading.Event) -> None:
    """Block *this thread* until ``host`` exists locally (wildcard: return at once).

    Runs inside the web server thread, never in the daemon's start-up path: the
    station keeps copying while the network is still coming up. ``pending`` is set
    as soon as an actual wait is needed, which is the caller's signal to stop
    waiting for a "listening" confirmation and get on with its work.
    """
    if is_wildcard_host(host) or address_available(host):
        return
    pending.set()
    _LOG.warning(
        "web.host %s is not an address of this machine (yet) -- the station is "
        "running normally; the web interface binds as soon as the address "
        "appears. Use web.host: 0.0.0.0 to serve every interface.", host,
    )
    delay, waited, logged = _BIND_RETRY_START, 0.0, 0.0
    while not address_available(host):
        time.sleep(delay)
        waited += delay
        delay = min(delay * 2, _BIND_RETRY_MAX)
        if waited - logged >= _BIND_WAIT_LOG_INTERVAL:
            logged = waited
            _LOG.warning("Still waiting for web.host %s (%.0f s so far).", host, waited)
    _LOG.info("web.host %s is up after %.0f s -- binding the web interface now.",
              host, waited)


def start_web_server(
    state: "StationState",
    host: str,
    port: int,
    config: "Optional[Config]" = None,
    browse: Any = None,
    transcode: Any = None,
    preview: Any = None,
    wifi_ap: Any = None,
) -> threading.Thread:
    """Start the web server in a daemon thread and return the thread.

    Waits for uvicorn to actually be listening before reporting success: the
    server runs in its own thread, so a failed bind (wrong ``web.host``, port
    taken) would otherwise leave only a stray uvicorn ERROR in the journal while
    the daemon happily logs the interface as up. A bind failure raises
    :class:`RuntimeError` here, so the caller can report the interface as down.

    The one bind failure that is NOT an error is a concrete ``host`` that does not
    exist *yet* (the boot race against DHCP): the server thread then waits for the
    address in the background and binds when it shows up, while this function
    returns immediately -- the station must never wait for a network to do its
    actual job. Everything else (port taken, no permission) still raises.

    Imports are local so the rest of the daemon runs even if FastAPI/uvicorn are
    not installed (the caller logs and continues). ``config`` enables auth and
    gates the optional file-browser/transcode features; ``browse``/``transcode``/
    ``preview`` are the managers backing them (``None`` when disabled/unavailable),
    ``wifi_ap`` the controller behind the interface's access-point switch.
    """
    import uvicorn

    from .app import create_app

    app = create_app(state, config, browse=browse, transcode=transcode,
                     preview=preview, wifi_ap=wifi_ap)
    uvicorn_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uvicorn_config)
    # We are not on the main thread, so uvicorn must not install signal handlers.
    server.install_signal_handlers = False

    # uvicorn logs the OSError and calls sys.exit(1) when the bind fails, which
    # raises SystemExit -- a BaseException -- inside this thread.
    failure: list[BaseException] = []
    pending = threading.Event()  # set while the host address is still missing

    def _serve() -> None:
        try:
            _wait_until_bindable(host, pending)
            server.run()
        except BaseException as exc:  # noqa: BLE001 - reported below, never swallowed
            failure.append(exc)
            if pending.is_set():
                # The caller returned long ago -- report the failure right here.
                _LOG.warning("Web interface could not be started: %s",
                             _bind_error(host, port, failure))

    thread = threading.Thread(target=_serve, name="copystation-web", daemon=True)
    thread.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            _LOG.info("Web interface listening on http://%s:%d", host, port)
            return thread
        if failure or not thread.is_alive():
            raise RuntimeError(_bind_error(host, port, failure))
        if pending.is_set():
            # Waiting for the address to appear -- do not hold up the daemon; the
            # server thread binds on its own and logs the outcome.
            return thread
        time.sleep(0.02)

    # Neither started nor dead: do not call it a failure, the daemon works fine
    # without the web UI and the socket may still come up.
    _LOG.warning(
        "Web interface did not confirm it is listening on %s:%d within %.0f s -- "
        "continuing; check http://%s:%d/ manually.",
        host, port, _STARTUP_TIMEOUT, host, port,
    )
    return thread


def _bind_error(host: str, port: int, failure: list[BaseException]) -> str:
    """Message for a server thread that died before it was listening."""
    exc = failure[0] if failure else None
    # uvicorn's own sys.exit(1) carries no detail -- it already logged the OSError.
    detail = f"{exc!r}" if exc is not None and not isinstance(exc, SystemExit) \
        else "see the uvicorn ERROR above"
    return (
        f"web server could not bind to {host}:{port} ({detail}). `web.port` must be "
        "free, and `web.host` an address of this machine -- use 0.0.0.0 to serve "
        "every interface. (An address that is merely not up YET is waited for, so "
        "this is a different problem.)"
    )
