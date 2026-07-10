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

import mimetypes
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..mounts import (
    BrowseError,
    MountFailed,
    NotFound,
    PathEscapesVolume,
    UnknownVolume,
)
from ..preview import PreviewUnavailable
from ..status import State
from ..transcode import TranscodeBusy, TranscodeError, TranscodeUnavailable, UnknownPreset


class TranscodeRequest(BaseModel):
    """POST /api/transcode body."""

    device: str
    path: str
    preset: str
    output_device: Optional[str] = None

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
    preview: Any = None,
) -> FastAPI:
    """Build the FastAPI app.

    ``config`` enables auth and gates the file/transcode features. ``browse`` is
    a :class:`copystation.mounts.BrowseManager` (file access), ``transcode`` a
    :class:`copystation.transcode.TranscodeManager` and ``preview`` a
    :class:`copystation.preview.PreviewManager`; any may be ``None`` when the
    corresponding feature is disabled or its dependencies are missing.
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
    preview_enabled = preview is not None and bool(getattr(preview, "available", False))

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
                    "delete": files_enabled and bool(getattr(browse, "allow_delete", False)),
                    "download": files_enabled and bool(getattr(browse, "allow_download", False)),
                    "preview": preview_enabled,
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
            # Send a real content type derived from the extension (e.g. video/mp4)
            # rather than a generic octet-stream: restricted clients like the
            # captive-portal webview ignore Content-Disposition and name the file
            # by MIME type, so octet-stream would save "clip.mp4" as ".bin".
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return FileResponse(str(target), filename=target.name, media_type=media_type)

        @app.get("/api/files/stream")
        def stream_file(
            device: str = Query(...),
            path: str = Query(...),
        ) -> FileResponse:
            # Same data exposure as a download (so it obeys the same allow_download
            # gate), but served **inline** with a real content type so the browser
            # plays it in place instead of downloading. Starlette's FileResponse
            # honours HTTP Range requests, so a <video> can seek and buffer without
            # fetching the whole (possibly multi-GB) file.
            try:
                target = browse.resolve_file(device, path)
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc
            media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return FileResponse(
                str(target),
                filename=target.name,
                media_type=media_type,
                content_disposition_type="inline",
            )

        @app.delete("/api/files")
        def delete_file(
            device: str = Query(...),
            path: str = Query(...),
        ) -> JSONResponse:
            # Serialise with the copy daemon and transcodes: refuse if busy, else
            # hold the operation lock for the (quick) delete so nothing else
            # mounts/writes the same device at the same time.
            if state.phase is State.COPYING:
                raise HTTPException(status_code=409, detail="a copy is in progress")
            if not state.operation_lock.acquire(blocking=False):
                raise HTTPException(status_code=409, detail="busy -- try again in a moment")
            try:
                browse.delete_file(device, path)
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc
            finally:
                state.operation_lock.release()
            return JSONResponse({"deleted": path})

    if preview is not None:

        @app.get("/api/files/preview-info")
        def preview_info(
            device: str = Query(...),
            path: str = Query(...),
        ) -> JSONResponse:
            # Whether the source plays in a browser as-is ("direct") or plays but
            # stutters ("transcode" -> the player hints to transcode for smooth
            # playback), plus its media properties.
            try:
                return JSONResponse(preview.info(device, path))
            except PreviewUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc

    if transcode is not None:

        @app.get("/api/transcode")
        def get_transcode() -> JSONResponse:
            return JSONResponse(transcode.snapshot())

        @app.get("/api/transcode/plan")
        def get_transcode_plan(
            device: str = Query(...),
            path: str = Query(...),
            preset: str = Query(...),
        ) -> JSONResponse:
            # File properties + which path (hw / hw+cpu / cpu) this preset will take
            # + a duration estimate from past jobs, for the file dialog.
            try:
                return JSONResponse(transcode.plan_for(device, path, preset))
            except TranscodeUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except UnknownPreset as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc

        @app.get("/api/transcode/folder-plan")
        def get_transcode_folder_plan(
            device: str = Query(...),
            path: str = Query(...),
            preset: str = Query(...),
        ) -> JSONResponse:
            # Per-file plan for every video in a folder, so the batch dialog can
            # show whether the files are handled uniformly (hw / hw+cpu / cpu).
            try:
                return JSONResponse(transcode.plan_folder(device, path, preset))
            except TranscodeUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except UnknownPreset as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc

        @app.post("/api/transcode/folder")
        def post_transcode_folder(req: TranscodeRequest) -> JSONResponse:
            # Queue one job per video file in the folder (a single preset for all).
            try:
                result = transcode.submit_folder(
                    req.device, req.path, req.preset, req.output_device
                )
            except TranscodeUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except TranscodeBusy as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except UnknownPreset as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc
            except TranscodeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(result)

        @app.post("/api/transcode")
        def post_transcode(req: TranscodeRequest) -> JSONResponse:
            try:
                job = transcode.submit(req.device, req.path, req.preset, req.output_device)
            except TranscodeUnavailable as exc:
                raise HTTPException(status_code=501, detail=str(exc)) from exc
            except TranscodeBusy as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except UnknownPreset as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except BrowseError as exc:
                raise _browse_http_error(exc) from exc
            except TranscodeError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return JSONResponse(job)

        @app.delete("/api/transcode/{job_id}")
        def cancel_transcode(job_id: int) -> JSONResponse:
            if not transcode.cancel(job_id):
                raise HTTPException(status_code=404, detail="no such active job")
            return JSONResponse({"canceled": job_id})

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app
