"""FastAPI application factory for the Copy_Station web interface.

Endpoints:
* ``GET /``            -- the static single-page frontend
* ``GET /api/status``  -- a JSON snapshot of the current StationState
* ``GET /api/settings``-- placeholder for future settings (documented, read-only)

The frontend polls ``/api/status`` (no WebSocket), which is robust against
network interfaces flapping: every poll is an independent request that simply
reconnects.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

if TYPE_CHECKING:
    from ..state import StationState

STATIC_DIR = Path(__file__).parent / "static"


def create_app(state: "StationState") -> FastAPI:
    app = FastAPI(title="Copy_Station", docs_url="/docs", redoc_url=None)

    @app.get("/api/status")
    def get_status() -> JSONResponse:
        return JSONResponse(state.snapshot())

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        # Placeholder: settings are read-only for now. A future version will
        # validate writes via a Pydantic model and persist them to config.yaml.
        return JSONResponse({"editable": False, "settings": {}})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
