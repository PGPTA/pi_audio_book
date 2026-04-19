"""First-run setup wizard: mic pick, cloud creds, admin password, hostname.

The wizard is a single page (`/setup`) that talks to a handful of JSON
endpoints under `/api/setup/*`. It writes `/etc/audiorec/config.toml`
through `common.config.save_config` and asks a tiny sudo helper to restart
the recorder/uploader so the new config takes effect.
"""
from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from common import config as cfg_mod
from common.config import (
    Config,
    is_admin_set,
    is_audio_configured,
    is_cloud_configured,
    save_config,
)

from .auth import COOKIE_NAME, SessionSigner, hash_password


log = logging.getLogger("audiorec.webapp.setup")


# --- provider presets shown in the UI ---------------------------------------
# Each entry is (id, label, region_placeholder, endpoint_template).
# `{region}` in endpoint_template is filled client-side from the region field.
PROVIDERS = [
    {
        "id": "b2",
        "label": "Backblaze B2",
        "region_example": "us-west-004",
        "endpoint_template": "https://s3.{region}.backblazeb2.com",
        "help": "Create a bucket + application key at https://secure.backblaze.com. Use the 'Endpoint' shown on the bucket page (e.g. s3.us-west-004.backblazeb2.com).",
    },
    {
        "id": "r2",
        "label": "Cloudflare R2",
        "region_example": "auto",
        "endpoint_template": "https://<account-id>.r2.cloudflarestorage.com",
        "help": "Create a bucket + API token at https://dash.cloudflare.com -> R2. Endpoint is https://<account-id>.r2.cloudflarestorage.com.",
    },
    {
        "id": "wasabi",
        "label": "Wasabi",
        "region_example": "us-east-1",
        "endpoint_template": "https://s3.{region}.wasabisys.com",
        "help": "Create a bucket + access key at https://console.wasabisys.com.",
    },
    {
        "id": "do",
        "label": "DigitalOcean Spaces",
        "region_example": "nyc3",
        "endpoint_template": "https://{region}.digitaloceanspaces.com",
        "help": "Create a Space + access key at https://cloud.digitalocean.com/spaces.",
    },
    {
        "id": "custom",
        "label": "Other S3-compatible",
        "region_example": "us-east-1",
        "endpoint_template": "https://...",
        "help": "Any S3-compatible endpoint. Enter the full URL yourself.",
    },
]

HELPER_BIN = "/opt/audiorec/bin/audiorec-setup-helper"


def build_setup_router(
    templates: Jinja2Templates,
    cfg_holder: "ConfigHolder",
    signer_holder: "SignerHolder",
) -> APIRouter:
    """Build the /setup router. `cfg_holder` is mutated in-place after each save
    so the rest of the webapp sees the new config without a restart.
    """
    router = APIRouter()

    # --- auth helpers (duplicated here to avoid a circular import) ---------
    def current_user(request: Request) -> str | None:
        cookie = request.cookies.get(COOKIE_NAME)
        return signer_holder.signer.verify_cookie(cookie)

    def require_setup_auth(request: Request) -> None:
        """Block setup endpoints once an admin exists unless a valid cookie is present.

        Before an admin is created the wizard is open (the Pi is only reachable
        over the user's LAN / WiFi-AP), which is how they bootstrap the first
        password. After that, setup is treated like any other admin page.
        """
        cfg = cfg_holder.cfg
        if is_admin_set(cfg):
            if current_user(request) is None:
                raise HTTPException(
                    status_code=status.HTTP_303_SEE_OTHER,
                    detail="login required",
                    headers={"Location": "/login"},
                )

    # --- page -------------------------------------------------------------
    @router.get("/setup")
    def setup_page(request: Request):
        cfg = cfg_holder.cfg
        # If setup is already complete AND the user is logged in, still allow
        # them to revisit (for reconfig). If not logged in, bounce to login.
        if is_admin_set(cfg) and current_user(request) is None:
            return RedirectResponse(url="/login?next=/setup", status_code=303)
        state = _state_for(cfg)
        return templates.TemplateResponse(
            request,
            "setup.html",
            {
                "state": state,
                "providers": PROVIDERS,
                "cfg": cfg,
            },
        )

    # --- step 1: create admin (open until admin exists) -------------------
    @router.post("/api/setup/admin")
    async def api_create_admin(request: Request):
        cfg = cfg_holder.cfg
        # Once an admin is set, require login to change it.
        if is_admin_set(cfg):
            if current_user(request) is None:
                raise HTTPException(status_code=401, detail="login required")

        body = await _json_body(request)
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        if not username or len(username) > 64:
            raise HTTPException(status_code=400, detail="username required (<=64 chars)")
        if len(password) < 6:
            raise HTTPException(status_code=400, detail="password must be at least 6 chars")

        cfg.web.username = username
        cfg.web.password_hash = hash_password(password)
        _persist(cfg_holder, signer_holder)

        # Log them in so the wizard can continue without an extra login step.
        cookie = signer_holder.signer.make_cookie(username)
        resp = JSONResponse({"ok": True, "state": _state_for(cfg)})
        resp.set_cookie(
            COOKIE_NAME, cookie,
            max_age=cfg.web.session_lifetime_s,
            httponly=True, samesite="lax", path="/",
        )
        return resp

    # --- step 2: microphone ----------------------------------------------
    @router.get("/api/setup/mics")
    def api_list_mics(request: Request):
        require_setup_auth(request)
        return {"mics": _detect_mics()}

    @router.post("/api/setup/test-mic")
    async def api_test_mic(request: Request):
        require_setup_auth(request)
        body = await _json_body(request)
        device = (body.get("device") or "").strip()
        if not device:
            raise HTTPException(status_code=400, detail="device required")
        rate = int(body.get("sample_rate") or 16000)
        channels = int(body.get("channels") or 1)
        duration = max(1, min(5, int(body.get("duration_s") or 2)))

        tmp = Path(tempfile.mkdtemp(prefix="audiorec-mictest-"))
        out = tmp / "test.wav"
        cmd = [
            "arecord", "-q",
            "-D", device,
            "-f", "S16_LE",
            "-r", str(rate),
            "-c", str(channels),
            "-d", str(duration),
            "-t", "wav",
            str(out),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=duration + 5)
        except subprocess.TimeoutExpired:
            raise HTTPException(status_code=500, detail="arecord hung; giving up")
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            raise HTTPException(status_code=400, detail=f"capture failed: {err[:400]}")
        if not out.exists() or out.stat().st_size < 44:  # 44 = WAV header
            raise HTTPException(status_code=500, detail="no audio captured")
        return FileResponse(out, media_type="audio/wav", filename="test.wav")

    @router.post("/api/setup/mic")
    async def api_save_mic(request: Request):
        require_setup_auth(request)
        cfg = cfg_holder.cfg
        body = await _json_body(request)
        device = (body.get("device") or "").strip()
        if not device:
            raise HTTPException(status_code=400, detail="device required")
        cfg.audio.device = device
        cfg.audio.sample_rate = int(body.get("sample_rate") or cfg.audio.sample_rate)
        cfg.audio.channels = int(body.get("channels") or cfg.audio.channels)
        cfg.audio.format = (body.get("format") or cfg.audio.format).strip()
        _persist(cfg_holder, signer_holder)
        _best_effort_helper("restart-recorder")
        return {"ok": True, "state": _state_for(cfg)}

    # --- step 3: cloud ----------------------------------------------------
    @router.post("/api/setup/test-cloud")
    async def api_test_cloud(request: Request):
        require_setup_auth(request)
        body = await _json_body(request)
        try:
            from uploader.wasabi import WasabiClient
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"boto3 not installed: {e}")

        from common.config import CloudConfig
        probe = CloudConfig(
            provider=(body.get("provider") or "").strip(),
            access_key=(body.get("access_key") or "").strip(),
            secret_key=(body.get("secret_key") or "").strip(),
            endpoint_url=(body.get("endpoint_url") or "").strip(),
            region=(body.get("region") or "").strip(),
            bucket=(body.get("bucket") or "").strip(),
            key_prefix=(body.get("key_prefix") or "recordings/").strip(),
        )
        missing = [
            k for k, v in (
                ("access_key", probe.access_key),
                ("secret_key", probe.secret_key),
                ("endpoint_url", probe.endpoint_url),
                ("bucket", probe.bucket),
            ) if not v
        ]
        if missing:
            raise HTTPException(status_code=400, detail=f"missing: {', '.join(missing)}")

        client = WasabiClient(probe, part_size_mb=5)
        try:
            client._client.head_bucket(Bucket=probe.bucket)  # noqa: SLF001
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"connection failed: {e}")
        return {"ok": True}

    @router.post("/api/setup/cloud")
    async def api_save_cloud(request: Request):
        require_setup_auth(request)
        cfg = cfg_holder.cfg
        body = await _json_body(request)
        c = cfg.cloud
        c.provider = (body.get("provider") or c.provider).strip()
        c.access_key = (body.get("access_key") or "").strip()
        c.secret_key = (body.get("secret_key") or "").strip()
        c.endpoint_url = (body.get("endpoint_url") or "").strip()
        c.region = (body.get("region") or "").strip()
        c.bucket = (body.get("bucket") or "").strip()
        c.key_prefix = (body.get("key_prefix") or c.key_prefix or "recordings/").strip()
        _persist(cfg_holder, signer_holder)
        _best_effort_helper("restart-uploader")
        return {"ok": True, "state": _state_for(cfg)}

    # --- step 4: hostname (optional) -------------------------------------
    @router.post("/api/setup/hostname")
    async def api_save_hostname(request: Request):
        require_setup_auth(request)
        cfg = cfg_holder.cfg
        body = await _json_body(request)
        raw = (body.get("hostname") or "").strip().lower()
        raw = re.sub(r"^https?://", "", raw)
        raw = raw.split("/", 1)[0]
        raw = raw.removesuffix(".local")
        if not re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", raw):
            raise HTTPException(status_code=400, detail="invalid hostname")
        cfg.web.hostname = f"{raw}.local"
        _persist(cfg_holder, signer_holder)
        try:
            subprocess.run(["sudo", "-n", HELPER_BIN, "set-hostname", raw], check=True, timeout=10)
        except subprocess.CalledProcessError as e:
            raise HTTPException(status_code=500, detail=f"helper failed: {e}")
        except FileNotFoundError:
            raise HTTPException(status_code=500, detail="sudo or helper missing")
        return {"ok": True, "state": _state_for(cfg), "hostname": cfg.web.hostname}

    # --- step 5: finish ---------------------------------------------------
    @router.post("/api/setup/finish")
    async def api_finish(request: Request):
        require_setup_auth(request)
        cfg = cfg_holder.cfg
        if not (is_admin_set(cfg) and is_audio_configured(cfg) and is_cloud_configured(cfg)):
            raise HTTPException(status_code=400, detail="finish: admin/mic/cloud must all be set")
        cfg.meta.setup_complete = True
        _persist(cfg_holder, signer_holder)
        _best_effort_helper("restart-recorder")
        _best_effort_helper("restart-uploader")
        return {"ok": True, "state": _state_for(cfg), "redirect": "/"}

    return router


# --- tiny mutable holders so the main app can reload config without tearing
# down the FastAPI app. The router keeps references to these, so all routes
# automatically see the latest values.
class ConfigHolder:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg


class SignerHolder:
    def __init__(self, signer: SessionSigner) -> None:
        self.signer = signer


def _persist(cfg_holder: ConfigHolder, signer_holder: SignerHolder) -> None:
    """Write config and rotate the session signer if the secret changed."""
    save_config(cfg_holder.cfg)
    # Reload from disk so in-memory config == on-disk config (canonicalization).
    cfg_holder.cfg = cfg_mod.load_config()
    signer_holder.signer = SessionSigner(
        cfg_holder.cfg.web.session_secret or "bootstrap",
        cfg_holder.cfg.web.session_lifetime_s,
    )


def _state_for(cfg: Config) -> dict[str, Any]:
    """JSON-safe summary the wizard UI uses to decide which steps are done."""
    return {
        "setup_complete": cfg.meta.setup_complete,
        "admin_set": is_admin_set(cfg),
        "admin_username": cfg.web.username,
        "mic_set": is_audio_configured(cfg),
        "mic_device": cfg.audio.device,
        "cloud_set": is_cloud_configured(cfg),
        "cloud_provider": cfg.cloud.provider,
        "cloud_bucket": cfg.cloud.bucket,
        "cloud_endpoint": cfg.cloud.endpoint_url,
        "hostname": cfg.web.hostname,
    }


_CARD_RE = re.compile(
    r"^card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\],\s+device\s+(\d+):\s+[^\[]*\[([^\]]+)\]"
)


def _detect_mics() -> list[dict[str, str]]:
    """Parse `arecord -l` into a list of {device, label} dicts; skip built-in bcm audio."""
    try:
        out = subprocess.check_output(["arecord", "-l"], stderr=subprocess.DEVNULL, timeout=5)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return []
    mics: list[dict[str, str]] = []
    for line in out.decode("utf-8", "replace").splitlines():
        m = _CARD_RE.match(line)
        if not m:
            continue
        card_num, card_id, card_name, dev_num, dev_name = m.groups()
        if re.search(r"bcm", card_id, re.I) or re.search(r"bcm", card_name, re.I):
            continue
        mics.append(
            {
                "device": f"hw:{card_num},{dev_num}",
                "label": f"{card_name} ({dev_name})",
            }
        )
    return mics


def _best_effort_helper(*args: str) -> None:
    """Invoke the setup helper via sudo, log-and-swallow errors."""
    try:
        subprocess.run(["sudo", "-n", HELPER_BIN, *args], check=True, timeout=10)
    except FileNotFoundError:
        log.warning("setup helper missing; skipping %s (dev env?)", args)
    except subprocess.CalledProcessError as e:
        log.warning("setup helper %s failed: %s", args, e)
    except subprocess.TimeoutExpired:
        log.warning("setup helper %s timed out", args)


async def _json_body(request: Request) -> dict:
    ct = request.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="expected JSON object")
        return data
    # Fall back to form so simple HTML POSTs still work.
    form = await request.form()
    return {k: (v if not hasattr(v, "filename") else "") for k, v in form.items()}
