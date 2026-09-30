import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from .database import AlertSettings, TelegramLinkCode

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Public URL the dashboard is reachable at from outside - also where
# Telegram's webhook calls come in.
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "").rstrip("/")

LINK_CODE_EXPIRE_MINUTES = 10
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O/1/I/L - avoids ambiguity if anyone reads it aloud/retypes it

# A random secret generated once per process and registered with Telegram
# via setWebhook, so incoming webhook calls can be verified as genuinely
# from Telegram (checked against the X-Telegram-Bot-Api-Secret-Token
# header) rather than a spoofed POST from anyone who finds the URL.
# Regenerated on every restart, which is fine - register_webhook() runs at
# startup each time too, so Telegram always has the current value.
WEBHOOK_SECRET = secrets.token_urlsafe(24)

_cached_bot_username: Optional[str] = None


def get_bot_username() -> Optional[str]:
    """Fetches (and caches for the process lifetime) the bot's own
    @username via Telegram's getMe, so the "Open Telegram" link can be
    built without a separate env var - derived straight from
    TELEGRAM_BOT_TOKEN. Returns None if no token is set or the call
    fails (e.g. bad token, no network)."""
    global _cached_bot_username
    if _cached_bot_username:
        return _cached_bot_username
    if not TELEGRAM_BOT_TOKEN:
        return None
    try:
        resp = requests.get(f"{TELEGRAM_API_BASE}/getMe", timeout=5)
        data = resp.json()
        if data.get("ok"):
            _cached_bot_username = data["result"]["username"]
            return _cached_bot_username
    except Exception:
        pass
    return None


def register_webhook(url_base_path: str = "/iot") -> bool:
    """Registers our webhook URL with Telegram - call once at startup.
    Does nothing (returns False) if TELEGRAM_BOT_TOKEN or APP_PUBLIC_URL
    aren't configured, since there's nowhere for Telegram to call or no
    bot to register for."""
    if not TELEGRAM_BOT_TOKEN or not APP_PUBLIC_URL:
        return False
    webhook_url = f"{APP_PUBLIC_URL}{url_base_path}/api/telegram/webhook"
    try:
        resp = requests.post(
            f"{TELEGRAM_API_BASE}/setWebhook",
            json={"url": webhook_url, "secret_token": WEBHOOK_SECRET},
            timeout=5,
        )
        return resp.ok and resp.json().get("ok", False)
    except Exception:
        return False


def _generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))


def create_link_code(db: Session, device_id: str) -> dict:
    """Generates a fresh one-time linking code for device_id, and
    opportunistically clears out expired codes so the table doesn't grow
    forever. Returns {"code", "bot_link" (None if the bot username can't
    be resolved), "expires_in_minutes"}."""
    now = datetime.utcnow()
    db.query(TelegramLinkCode).filter(TelegramLinkCode.expires_at < now).delete()

    code = _generate_code()
    for _ in range(5):  # astronomically unlikely to collide, but cheap to guard
        if not db.query(TelegramLinkCode).filter(TelegramLinkCode.code == code).first():
            break
        code = _generate_code()

    db.add(
        TelegramLinkCode(
            code=code,
            device_id=device_id,
            expires_at=now + timedelta(minutes=LINK_CODE_EXPIRE_MINUTES),
        )
    )
    db.commit()

    username = get_bot_username()
    bot_link = f"https://t.me/{username}?start={code}" if username else None
    return {"code": code, "bot_link": bot_link, "expires_in_minutes": LINK_CODE_EXPIRE_MINUTES}


def _reply(chat_id, text: str) -> None:
    if not TELEGRAM_BOT_TOKEN:
        return
    try:
        requests.post(f"{TELEGRAM_API_BASE}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception:
        pass


def handle_incoming_update(db: Session, update: dict) -> None:
    """Processes one Telegram webhook update. Looks for a "/start <code>"
    message (what Telegram sends when someone taps a t.me/<bot>?start=xxx
    deep link), matches it against a pending link code, and if valid saves
    that message's own chat_id onto the matching device's AlertSettings -
    no manual chat_id entry needed. Replies in the chat either way so the
    person gets clear feedback. Never raises - a malformed or unexpected
    update must not break the webhook endpoint."""
    try:
        message = update.get("message") or update.get("channel_post")
        if not message:
            return
        text = (message.get("text") or "").strip()
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            return

        parts = text.split(maxsplit=1)
        code = parts[1].strip() if text.startswith("/start") and len(parts) > 1 else ""
        if not code:
            _reply(chat_id, "ส่งรหัสเชื่อมต่อที่ได้จากหน้าเว็บมาที่นี่ เพื่อเชื่อมต่อการแจ้งเตือนของอุปกรณ์คุณ")
            return

        now = datetime.utcnow()
        link = (
            db.query(TelegramLinkCode)
            .filter(TelegramLinkCode.code == code.upper(), TelegramLinkCode.expires_at >= now)
            .first()
        )
        if not link:
            _reply(chat_id, "รหัสเชื่อมต่อไม่ถูกต้องหรือหมดอายุแล้ว กลับไปกดปุ่ม \"เชื่อมต่อ Telegram\" ใหม่จากหน้าเว็บ")
            return

        device_id = link.device_id
        settings = db.query(AlertSettings).filter(AlertSettings.device_id == device_id).first()
        if settings is None:
            settings = AlertSettings(device_id=device_id, enabled=True, cooldown_minutes=15)
            db.add(settings)
        settings.telegram_chat_id = str(chat_id)

        db.delete(link)
        db.commit()

        _reply(chat_id, f"✅ เชื่อมต่อสำเร็จ! อุปกรณ์: {device_id}\nจะได้รับแจ้งเตือนที่แชทนี้เมื่อค่าผิดปกติ")
    except Exception:
        db.rollback()
