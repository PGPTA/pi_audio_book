"""FastAPI web UI for the audio recorder.

Password-protected dashboard you can load on your phone over the local WiFi.
Shows live status, lets you browse recordings, stream local WAVs, fetch the
uploaded Opus from Wasabi via a short-lived presigned URL, or delete a
recording.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from common import db
from common.config import Config, load_config

from .auth import COOKIE_NAME, SessionSigner, verify_password


log = logging.getLogger("audiorec.webapp")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or load_config()
    signer = SessionSigner(cfg.web.session_secret, cfg.web.session_lifetime_s)
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["filesizeformat"] = _format_bytes
    templates.env.filters["duration"] = _format_duration

    app = FastAPI(title="Audiorec", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # --- per-request helpers ---
    def get_conn():
        conn = db.connect(cfg.paths.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_user(request: Request) -> str:
        cookie = request.cookies.get(COOKIE_NAME)
        user = signer.verify_cookie(cookie)
        if user is None:
            raise HTTPException(
                status_code=status.HTTP_303_SEE_OTHER,
                detail="login required",
                headers={"Location": "/login"},
            )
        return user

    # --- routes ---
    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login_submit(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        ok = username == cfg.web.username and verify_password(password, cfg.web.password_hash)
        if not ok:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"error": "Invalid username or password."},
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        resp = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        resp.set_cookie(
            COOKIE_NAME,
            signer.make_cookie(username),
            max_age=cfg.web.session_lifetime_s,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
        resp.delete_cookie(COOKIE_NAME, path="/")
        return resp

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn=Depends(get_conn), _user: str = Depends(require_user)):
        current = db.current_recording(conn)
        last_up = db.last_uploaded(conn)
        pending = db.count_by_status(conn, db.STATUS_PENDING_UPLOAD) + db.count_by_status(
            conn, db.STATUS_UPLOADING
        )
        failed = db.count_by_status(conn, db.STATUS_FAILED)
        total = db.count_recordings(conn)
        disk = _disk_usage(cfg.paths.data_dir)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "hostname": cfg.web.hostname,
                "current": current,
                "last_uploaded": last_up,
                "pending_count": pending,
                "failed_count": failed,
                "total": total,
                "disk": disk,
            },
        )

    @app.get("/recordings", response_class=HTMLResponse)
    def recordings_list(
        request: Request,
        page: int = 1,
        conn=Depends(get_conn),
        _user: str = Depends(require_user),
    ):
        page = max(1, page)
        per_page = 25
        offset = (page - 1) * per_page
        items = db.list_recordings(conn, limit=per_page, offset=offset)
        total = db.count_recordings(conn)
        pages = max(1, (total + per_page - 1) // per_page)
        return templates.TemplateResponse(
            request,
            "recordings.html",
            {
                "items": items,
                "page": page,
                "pages": pages,
                "total": total,
            },
        )

    @app.get("/recordings/{rec_id}/stream")
    def stream_local(
        rec_id: str,
        conn=Depends(get_conn),
        _user: str = Depends(require_user),
    ):
        try:
            rec = db.get_recording(conn, rec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="recording not found")
        path = cfg.paths.recordings_dir / rec.filename
        if not path.exists():
            raise HTTPException(status_code=410, detail="local WAV pruned; use /cloud")
        return FileResponse(path, media_type="audio/wav", filename=rec.filename)

    @app.get("/recordings/{rec_id}/cloud")
    def redirect_to_cloud(
        rec_id: str,
        conn=Depends(get_conn),
        _user: str = Depends(require_user),
    ):
        try:
            rec = db.get_recording(conn, rec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="recording not found")
        if not rec.cloud_key:
            raise HTTPException(status_code=409, detail="not uploaded yet")

        # Lazy import so the webapp doesn't pull boto3 at startup if the user
        # hasn't configured Wasabi yet.
        from uploader.wasabi import WasabiClient
        client = WasabiClient(cfg.wasabi, part_size_mb=cfg.upload.multipart_part_size_mb)
        url = client.presign_get(rec.cloud_key, expires_s=300)
        return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)

    @app.post("/recordings/{rec_id}/delete")
    def delete_recording(
        rec_id: str,
        conn=Depends(get_conn),
        _user: str = Depends(require_user),
    ):
        try:
            rec = db.get_recording(conn, rec_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="recording not found")
        if rec.status in (db.STATUS_RECORDING, db.STATUS_UPLOADING):
            raise HTTPException(status_code=409, detail="cannot delete while in-flight")

        # Delete cloud copy first (best-effort).
        if rec.cloud_key:
            try:
                from uploader.wasabi import WasabiClient
                client = WasabiClient(cfg.wasabi, part_size_mb=cfg.upload.multipart_part_size_mb)
                client.delete(rec.cloud_key)
            except Exception:
                log.exception("Failed to delete cloud copy for %s", rec.id)

        # Then the local WAV.
        path = cfg.paths.recordings_dir / rec.filename
        try:
            if path.exists():
                path.unlink()
        except OSError:
            log.exception("Failed to delete local file %s", path)

        db.delete_recording(conn, rec.id)
        return RedirectResponse(url="/recordings", status_code=status.HTTP_303_SEE_OTHER)

    @app.get("/api/status")
    def api_status(conn=Depends(get_conn), _user: str = Depends(require_user)):
        current = db.current_recording(conn)
        last_up = db.last_uploaded(conn)
        return {
            "recording": current is not None,
            "current_id": current.id if current else None,
            "pending": db.count_by_status(conn, db.STATUS_PENDING_UPLOAD)
            + db.count_by_status(conn, db.STATUS_UPLOADING),
            "failed": db.count_by_status(conn, db.STATUS_FAILED),
            "total": db.count_recordings(conn),
            "last_uploaded_at": last_up.uploaded_at.isoformat() if last_up and last_up.uploaded_at else None,
            "disk_free_bytes": _disk_usage(cfg.paths.data_dir)["free"],
        }

    # Map auth redirect exceptions to 303s.
    @app.exception_handler(HTTPException)
    def _auth_redirects(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_303_SEE_OTHER and "Location" in (exc.headers or {}):
            return RedirectResponse(url=exc.headers["Location"], status_code=303)
        return Response(
            content=exc.detail if isinstance(exc.detail, str) else str(exc.detail),
            status_code=exc.status_code,
            media_type="text/plain",
        )

    return app


def _disk_usage(path: Path) -> dict:
    try:
        total, used, free = shutil.disk_usage(path)
    except FileNotFoundError:
        path.mkdir(parents=True, exist_ok=True)
        total, used, free = shutil.disk_usage(path)
    return {"total": total, "used": used, "free": free}


def _format_bytes(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def main() -> int:
    import uvicorn

    logging.basicConfig(
        level=os.environ.get("AUDIOREC_LOG", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    cfg = load_config()
    app = create_app(cfg)
    uvicorn.run(
        app,
        host=cfg.web.host,
        port=cfg.web.port,
        workers=1,
        log_level=os.environ.get("AUDIOREC_LOG", "info").lower(),
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
