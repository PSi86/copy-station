"""Optional local web interface (FastAPI).

Started in a background daemon thread by the main daemon when ``web.enabled`` is
set. Binds to ``0.0.0.0`` so it serves every network interface, including ones
that come up or go down at runtime -- a wildcard listening socket needs no
per-interface rebinding.
"""

from __future__ import annotations

import logging
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


def start_web_server(
    state: "StationState",
    host: str,
    port: int,
    config: "Optional[Config]" = None,
    browse: Any = None,
    transcode: Any = None,
    preview: Any = None,
) -> threading.Thread:
    """Start the web server in a daemon thread and return the thread.

    Waits for uvicorn to actually be listening before reporting success: the
    server runs in its own thread, so a failed bind (wrong ``web.host``, port
    taken) would otherwise leave only a stray uvicorn ERROR in the journal while
    the daemon happily logs the interface as up. A bind failure raises
    :class:`RuntimeError` here, so the caller can report the interface as down.

    Imports are local so the rest of the daemon runs even if FastAPI/uvicorn are
    not installed (the caller logs and continues). ``config`` enables auth and
    gates the optional file-browser/transcode features; ``browse``/``transcode``/
    ``preview`` are the managers backing them (``None`` when disabled/unavailable).
    """
    import uvicorn

    from .app import create_app

    app = create_app(state, config, browse=browse, transcode=transcode, preview=preview)
    uvicorn_config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(uvicorn_config)
    # We are not on the main thread, so uvicorn must not install signal handlers.
    server.install_signal_handlers = False

    # uvicorn logs the OSError and calls sys.exit(1) when the bind fails, which
    # raises SystemExit -- a BaseException -- inside this thread.
    failure: list[BaseException] = []

    def _serve() -> None:
        try:
            server.run()
        except BaseException as exc:  # noqa: BLE001 - reported below, never swallowed
            failure.append(exc)

    thread = threading.Thread(target=_serve, name="copystation-web", daemon=True)
    thread.start()

    deadline = time.monotonic() + _STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            _LOG.info("Web interface listening on http://%s:%d", host, port)
            return thread
        if failure or not thread.is_alive():
            raise RuntimeError(_bind_error(host, port, failure))
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
        f"web server could not bind to {host}:{port} ({detail}). `web.host` must be "
        "an address that exists on this machine -- use 0.0.0.0 to serve every "
        "interface -- and `web.port` must be free."
    )
