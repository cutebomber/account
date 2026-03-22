"""
Mini App backend — FastAPI server that serves the web app and
exposes a small JSON API for the frontend.

The Mini App frontend talks to this server; the server talks to
the shared SessionManager instance (passed in at startup).

Run alongside the bot:
    uvicorn miniapp_server:app --host 0.0.0.0 --port 8080
"""

import hashlib
import hmac
import json
import logging
import urllib.parse
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from config import MINIAPP_SECRET, OWNER_ID
import config

logger = logging.getLogger(__name__)

app = FastAPI(title="TG Session Bot Mini App", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Injected at startup by bot.py
_session_manager = None

def set_session_manager(sm):
    global _session_manager
    _session_manager = sm


# ── Telegram WebApp auth validation ──────────────────────────────────────────

def _validate_telegram_init_data(init_data: str) -> Optional[dict]:
    """
    Validate the Telegram Mini App initData string.
    Returns the parsed user dict or None if invalid.
    """
    try:
        parsed = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
        received_hash = parsed.pop("hash", "")

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(
            b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256
        ).digest()
        expected_hash = hmac.new(
            secret_key, data_check_string.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected_hash, received_hash):
            return None

        user_str = parsed.get("user", "{}")
        return json.loads(user_str)
    except Exception:
        return None


def _require_owner(init_data: str):
    user = _validate_telegram_init_data(init_data)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid Telegram auth")
    if user.get("id") != OWNER_ID:
        raise HTTPException(status_code=403, detail="Forbidden")
    return user


# ── REST API ──────────────────────────────────────────────────────────────────

@app.get("/api/accounts")
async def get_accounts(x_init_data: str = Header(...)):
    _require_owner(x_init_data)
    return {"accounts": _session_manager.accounts}


class SendMessageRequest(BaseModel):
    phone: str
    recipient: str
    text: str


@app.post("/api/send")
async def send_message(body: SendMessageRequest, x_init_data: str = Header(...)):
    _require_owner(x_init_data)
    try:
        await _session_manager.send_message(body.phone, body.recipient, body.text)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


class DisconnectRequest(BaseModel):
    phone: str


@app.post("/api/disconnect")
async def disconnect_account(body: DisconnectRequest, x_init_data: str = Header(...)):
    _require_owner(x_init_data)
    await _session_manager.disconnect_account(body.phone)
    return {"ok": True}


# ── Serve static Mini App files ───────────────────────────────────────────────

app.mount("/miniapp", StaticFiles(directory="miniapp", html=True), name="miniapp")


@app.get("/")
async def root():
    return FileResponse("miniapp/index.html")
