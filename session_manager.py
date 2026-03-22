"""
SessionManager
- Keeps a dict of active Telethon clients keyed by phone number
- Persists sessions as encrypted .session files in SESSIONS_DIR
- Forwards incoming OTPs (and any 2-FA codes) to the bot owner
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, Optional

from aiogram import Bot
from telethon import TelegramClient, events
from telethon.sessions import StringSession

import config

logger = logging.getLogger(__name__)

# Patterns that look like OTPs / login codes
OTP_PATTERN = re.compile(
    r"""(
        \b\d{4,8}\b                          # plain numeric codes
        | [A-Z0-9]{4,8}-[A-Z0-9]{2,6}       # hyphenated codes  e.g. ABC12-XY
        | login\s+code[:\s]+[\w\-]+          # "login code: XXXX"
        | verification\s+code[:\s]+[\w\-]+   # "verification code: XXXX"
        | one[- ]time\s+(password|code)[:\s]+[\w\-]+
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# Senders whose messages are ALWAYS forwarded (Telegram service accounts)
ALWAYS_FORWARD_SENDERS = {
    777000,   # Telegram official
}


def _meta_path(phone: str) -> Path:
    return Path(config.SESSIONS_DIR) / f"{phone}.json"


def _session_path(phone: str) -> Path:
    return Path(config.SESSIONS_DIR) / f"{phone}.session"


class SessionManager:
    def __init__(self, bot: Bot, owner_id: int):
        self.bot = bot
        self.owner_id = owner_id
        self._clients: Dict[str, TelegramClient] = {}
        Path(config.SESSIONS_DIR).mkdir(parents=True, exist_ok=True)

    # ── Public API ────────────────────────────────────────────────────────────

    @property
    def accounts(self) -> list[dict]:
        """Return metadata list for all saved accounts."""
        result = []
        for phone in self._saved_phones():
            meta = self._load_meta(phone)
            meta["connected"] = phone in self._clients
            result.append(meta)
        return result

    def is_connected(self, phone: str) -> bool:
        return phone in self._clients

    async def resume_all(self):
        """Re-connect every account that has a saved session file."""
        for phone in self._saved_phones():
            try:
                await self._start_client(phone)
            except Exception as exc:
                logger.warning("Could not resume %s: %s", phone, exc)

    async def begin_login(self, phone: str) -> str:
        """
        Start the login flow for *phone*.
        Returns a confirmation message; the OTP must be submitted via complete_login().
        Raises on failure.
        """
        client = TelegramClient(
            str(_session_path(phone)),
            config.API_ID,
            config.API_HASH,
        )
        await client.connect()
        await client.send_code_request(phone)
        # Temporarily store so complete_login can reuse the same client
        self._clients[f"__pending__{phone}"] = client
        return f"✅ OTP sent to *{phone}*. Use /otp <code> to complete login."

    async def complete_login(self, phone: str, code: str, password: str = "") -> str:
        """
        Finish the Telethon login with the OTP (and optional 2-FA password).
        Returns a success message or raises.
        """
        key = f"__pending__{phone}"
        client: Optional[TelegramClient] = self._clients.pop(key, None)
        if client is None:
            raise ValueError("No pending login for that number. Use /addaccount first.")

        try:
            await client.sign_in(phone, code)
        except Exception as two_fa_err:
            if "two-steps" in str(two_fa_err).lower() or "password" in str(two_fa_err).lower():
                if not password:
                    # Put client back and ask for password
                    self._clients[f"__2fa__{phone}"] = client
                    raise ValueError("2FA_REQUIRED")
                await client.sign_in(password=password)
            else:
                await client.disconnect()
                raise

        await self._finalise_client(phone, client)
        return f"🔐 Account *{phone}* saved and connected!"

    async def complete_2fa(self, phone: str, password: str) -> str:
        key = f"__2fa__{phone}"
        client: Optional[TelegramClient] = self._clients.pop(key, None)
        if client is None:
            raise ValueError("No pending 2FA for that number.")
        await client.sign_in(password=password)
        await self._finalise_client(phone, client)
        return f"🔐 Account *{phone}* saved and connected (2FA ok)!"

    async def disconnect_account(self, phone: str):
        client = self._clients.pop(phone, None)
        if client:
            await client.disconnect()
        _session_path(phone).unlink(missing_ok=True)
        _meta_path(phone).unlink(missing_ok=True)

    async def send_message(self, phone: str, recipient: str, text: str):
        """Send a message FROM the saved account identified by *phone*."""
        client = self._clients.get(phone)
        if not client:
            raise ValueError(f"Account {phone} is not connected.")
        await client.send_message(recipient, text)

    async def stop_all(self):
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _saved_phones(self) -> list[str]:
        return [p.stem for p in Path(config.SESSIONS_DIR).glob("*.json")]

    def _load_meta(self, phone: str) -> dict:
        path = _meta_path(phone)
        if path.exists():
            with open(path) as f:
                return json.load(f)
        return {"phone": phone, "name": "Unknown"}

    def _save_meta(self, phone: str, data: dict):
        with open(_meta_path(phone), "w") as f:
            json.dump(data, f, indent=2)

    async def _start_client(self, phone: str):
        client = TelegramClient(
            str(_session_path(phone)),
            config.API_ID,
            config.API_HASH,
        )
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning("%s session exists but is no longer authorised", phone)
            await client.disconnect()
            return
        await self._finalise_client(phone, client, is_resume=True)

    async def _finalise_client(self, phone: str, client: TelegramClient, is_resume=False):
        """Attach event handlers and store the client."""
        me = await client.get_me()
        name = f"{me.first_name or ''} {me.last_name or ''}".strip() or phone
        self._save_meta(phone, {
            "phone": phone,
            "name": name,
            "username": me.username or "",
            "user_id": me.id,
        })

        # Attach OTP-forwarding listener
        @client.on(events.NewMessage(incoming=True))
        async def _on_message(event):
            await self._handle_incoming(phone, name, event)

        self._clients[phone] = client
        if not is_resume:
            logger.info("New account connected: %s (%s)", name, phone)
        else:
            logger.info("Resumed account: %s (%s)", name, phone)

    async def _handle_incoming(self, phone: str, account_name: str, event):
        """Forward OTPs and messages from trusted senders to the owner."""
        try:
            sender = await event.get_sender()
            sender_id = getattr(sender, "id", None)
            text = event.raw_text or ""

            is_otp_sender = sender_id in ALWAYS_FORWARD_SENDERS
            looks_like_otp = bool(OTP_PATTERN.search(text))

            if not (is_otp_sender or looks_like_otp):
                return

            sender_name = getattr(sender, "first_name", None) or getattr(sender, "title", None) or str(sender_id)
            preview = text[:300] + ("…" if len(text) > 300 else "")

            msg = (
                f"📨 *New message on account: {account_name}* (`{phone}`)\n"
                f"👤 From: {sender_name}\n\n"
                f"`{preview}`"
            )
            await self.bot.send_message(self.owner_id, msg, parse_mode="Markdown")
        except Exception as exc:
            logger.error("Error in _handle_incoming for %s: %s", phone, exc)
