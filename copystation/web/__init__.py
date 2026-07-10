"""Optional local web interface (FastAPI).

Started in a background daemon thread by the main daemon when ``web.enabled`` is
set. Binds to ``0.0.0.0`` so it serves every network interface, including ones
that come up or go down at runtime -- a wildcard listening socket needs no
per-interface rebinding.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from ..config import Config
    from ..state import StationState

_LOG = logging.getLogger("copystation.web")


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

    thread = threading.Thread(target=server.run, name="copystation-web", daemon=True)
    thread.start()
    _LOG.info("Web interface listening on http://%s:%d", host, port)
    return thread
