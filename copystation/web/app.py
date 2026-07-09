"""FastAPI application factory for the Copy_Station web interface.

Endpoints:
* ``GET /``                   -- the static single-page frontend
* ``GET /api/status``         -- a JSON snapshot of the current StationState
* ``GET /api/settings``       -- capabilities of this build (files/transcode on?)
* ``GET /api/volumes``        -- attached USB volumes (file browser; never the OS)
* ``GET /api/files``          -- directory listing of one attached volume
* ``GET /api/files/download`` -- stream one file from an attached volume
* ``GET/POST/DELETE /api/transcode`` -- video transcode jobs

The frontend polls ``/api/status`` (no WebSocket), which is robust against
network interfaces flapping: every poll is an independent request that simply
reconnects.

When ``web.auth.enabled`` is set the whole interface is behind HTTP Basic auth
(the static assets under ``/static`` stay open -- they carry no data). Auth is
off by default, so existing status-only deployments are unaffected.
"""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from ..mounts import (
    BrowseError,
    MountFailed,
    NotFound,
    PathEscapesVolume,
    UnknownVolume,
)

if TYPE_CHECKING:
    from ..config import Config
    from ..state import StationState


# BrowseError subclass -> HTTP status code for the file endpoints.
_BROWSE_HTTP_STATUS = {
    UnknownVolume: 404,
    NotFound: 404,
    PathEscapesVolume: 403,
    MountFailed: 503,
}


def _browse_http_error(exc: BrowseError) -> HTTPException:
    return HTTPException(status_code=_BROWSE_HTTP_STATUS.get(type(exc), 400), detail=str(exc))

STATIC_DIR = Path(__file__).parent / "static"


def _build_auth_dependency(config: "Optional[Config]") -> Callable:
    """Return a FastAPI dependency enforcing HTTP Basic auth, or a no-op.

    Fail-safe: if auth is enabled but no password is configured, every request
    is rejected (503) rather than silently left open.
    """
    auth_cfg: dict[str, Any] = {}
    if config is not None:
        auth_cfg = (config.get("web", {}) or {}).get("auth", {}) or {}

    if not auth_cfg.get("enabled"):
        def _no_auth() -> None:
            return None

        return _no_auth

    username = str(auth_cfg.get("username", "admin"))
    password = str(auth_cfg.get("password", ""))
    security = HTTPBasic()

    def _check(credentials: HTTPBasicCredentials = Depends(security)) -> str:
        if not password:
            raise HTTPException(status_code=503, detail="Web auth enabled but no password set")
        user_ok = secrets.compare_digest(credentials.username, username)
        pass_ok = secrets.compare_digest(credentials.password, password)
        if not (user_ok and pass_ok):
            raise HTTPException(
                status_code=401,
                detail="Invalid credentials",
                headers={"WWW-Authenticate": "Basic"},
            )
        return credentials.username

    return _check


def create_app(
    state: "StationState",
    config: "Optional[Config]" = None,
    browse: Any = None,
    transcode: Any = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``config`` enables auth and gates the file/transcode features. ``browse`` is
    a :class:`copystation.mounts.BrowseManager` (file access) and ``transcode`` a
    :class:`copystation.transcode.TranscodeManager`; either may be ``None`` when
    the corresponding feature is disabled or its dependencies are missing.
    """
    auth = _build_auth_dependency(config)
    app = FastAPI(
        title="Copy_Station",
        docs_url="/docs",
        redoc_url=None,
        dependencies=[Depends(auth)],
    )

    files_enabled = browse is not None
    transcode_enabled = transcode is not None

    @app.get("/api/status")
    def get_status() -> JSONResponse:
        return JSONResponse(state.snapshot())

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        # Capabilities of this build, so the frontend can show/hide panels.
        return JSONResponse(
            {
                "editable": False,
                "settings": {},
                "features": {
                    "files": files_enabled,
                    "transcode": transcode_enabled,
                },
            }
        )

    if browse is not None:

        @app.get("/api/volumes")
        def get_volumes() -> JSONResponse:
            # Attached USB mass storage only -- the OS/root device is excluded by
            # copystation.volumes, so it can never be browsed.
            return JSONResponse({"volumes": browse.list_volumes()})

        @app.get("/api/files")
        def get_files(
            device: str = Query(..., description="sys_name from /api/volumes"),
            path: str = Query("", description="path relative to the volume root"),
        ) -> JSONResponse:
            try:
                return JSONResponse(browse.list_dir(device, path))
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc

        @app.get("/api/files/download")
        def download_file(
            device: str = Query(...),
            path: str = Query(...),
        ) -> FileResponse:
            try:
                target = browse.resolve_file(device, path)
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc
            return FileResponse(
                str(target), filename=target.name, media_type="application/octet-stream"
            )

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
