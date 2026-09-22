import os
import secrets
from datetime import datetime, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from .auth import DEVICE_VIEW_LINK_HOURS, create_device_view_token
from .database import AlertSettings, AlertState, ShortLink

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

# Public URL the dashboard is reachable at from outside (e.g.
# "http://1.2.3.4:3005"), used to build a "view graph" link inside Telegram
# alert messages. Leave unset to send alerts without a link.
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "").rstrip("/")


def _create_short_link(db: Session, target_url: str, expire_hours: int = DEVICE_VIEW_LINK_HOURS) -> str:
    """Store target_url under a short random id and return that id. Also
    opportunistically clears out expired links so the table doesn't grow
    forever - no separate cleanup job needed for this volume of traffic."""
    now = datetime.utcnow()
    db.query(ShortLink).filter(ShortLink.expires_at < now).delete()

    short_id = secrets.token_urlsafe(6)  # ~8 URL-safe chars
    db.add(
        ShortLink(
            short_id=short_id,
            target_url=target_url,
            expires_at=now + timedelta(hours=expire_hours),
        )
    )
    db.commit()
    return short_id


def device_link(db: Session, device_id: str) -> str:
    """Build the 'view graph' deep link for a device, or '' if no public
    URL is configured. Embeds a short-lived, read-only, device-scoped view
    token (see auth.create_device_view_token) so tapping the link from
    Telegram opens straight to that device's graph without a login prompt.
    The link handed out is a short self-hosted redirect (/iot/s/<id>), not
    the full URL with the token in it - much friendlier inside a Telegram
    message. Used both for real Telegram alert messages and the test-alert
    button."""
    if not APP_PUBLIC_URL:
        return ""
    token = create_device_view_token(device_id)
    full_url = f"{APP_PUBLIC_URL}/iot/?device={device_id}&token={token}"
    short_id = _create_short_link(db, full_url)
    return f"{APP_PUBLIC_URL}/iot/s/{short_id}"


def send_telegram_message(chat_ids: str, text: str) -> bool:
    """Best-effort send to one or more chat IDs (comma/whitespace separated,
    e.g. "111,222,333" to notify several people/groups for the same device).
    Never raises - a Telegram outage must not break sensor ingestion or the
    API. Returns True if delivery succeeded to at least one recipient."""
    if not TELEGRAM_BOT_TOKEN or not chat_ids:
        return False

    ids = [c.strip() for c in chat_ids.replace(";", ",").split(",") if c.strip()]
    if not ids:
        return False

    any_ok = False
    for chat_id in ids:
        try:
            resp = requests.post(
                TELEGRAM_API_URL,
                json={"chat_id": chat_id, "text": text},
                timeout=5,
            )
            if resp.ok:
                any_ok = True
        except Exception:
            continue
    return any_ok


def get_settings(db: Session, device_id: str) -> AlertSettings:
    settings = db.query(AlertSettings).filter(AlertSettings.device_id == device_id).first()
    if settings is None:
        settings = AlertSettings(device_id=device_id, enabled=True, cooldown_minutes=15)
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def update_settings(db: Session, device_id: str, payload) -> AlertSettings:
    settings = get_settings(db, device_id)
    settings.temp_high = payload.temp_high
    settings.temp_high_clear = payload.temp_high_clear
    settings.temp_low = payload.temp_low
    settings.temp_low_clear = payload.temp_low_clear
    settings.humid_high = payload.humid_high
    settings.humid_high_clear = payload.humid_high_clear
    settings.humid_low = payload.humid_low
    settings.humid_low_clear = payload.humid_low_clear
    settings.telegram_chat_id = payload.telegram_chat_id
    settings.enabled = payload.enabled
    settings.cooldown_minutes = payload.cooldown_minutes
    db.commit()
    db.refresh(settings)
    return settings


def _get_or_create_state(db: Session, device_id: str) -> AlertState:
    state = db.query(AlertState).filter(AlertState.device_id == device_id).first()
    if state is None:
        state = AlertState(device_id=device_id)
        db.add(state)
        db.commit()
        db.refresh(state)
    return state


def _evaluate_metric(
    value: float,
    high: Optional[float],
    high_clear: Optional[float],
    low: Optional[float],
    low_clear: Optional[float],
    active: bool,
    direction: Optional[str],
):
    """Hysteresis state machine for one metric (temperature or humidity).

    Returns (new_active, new_direction, event) where event is one of
    "breach_high", "breach_low", "clear", or None (no change to report).

    While active, we only ever check the CLEAR threshold for whichever
    direction triggered the alarm - not the breach threshold - so a value
    flapping just above/below the breach line does not re-trigger or
    "clear" on every reading. If a *_clear value isn't set it defaults to
    the breach threshold itself (no hysteresis, old behaviour)."""
    if active:
        if direction == "high":
            clear_at = high_clear if high_clear is not None else high
            if clear_at is not None and value < clear_at:
                return False, None, "clear"
            return True, direction, None
        elif direction == "low":
            clear_at = low_clear if low_clear is not None else low
            if clear_at is not None and value > clear_at:
                return False, None, "clear"
            return True, direction, None
        # Shouldn't happen, but don't get stuck if direction is missing.
        return False, None, "clear"

    if high is not None and value > high:
        return True, "high", "breach_high"
    if low is not None and value < low:
        return True, "low", "breach_low"
    return False, None, None


def check_and_alert(
    db: Session, device_id: str, temperature: Optional[float], humidity: Optional[float]
) -> None:
    """Called right after a sensor point is written. Sends a Telegram alert
    the moment a threshold is first crossed (edge-trigger), a 'back to
    normal' message once it clears past the hysteresis threshold, and never
    re-alerts while still in the same active breach."""
    settings = get_settings(db, device_id)
    if not settings.enabled or not settings.telegram_chat_id:
        return

    state = _get_or_create_state(db, device_id)
    now = datetime.utcnow()
    cooldown = timedelta(minutes=settings.cooldown_minutes or 0)
    link = device_link(db, device_id)
    link_suffix = f"\n{link}" if link else ""

    if temperature is not None:
        new_active, new_direction, event = _evaluate_metric(
            temperature,
            settings.temp_high,
            settings.temp_high_clear,
            settings.temp_low,
            settings.temp_low_clear,
            state.temp_alert_active,
            state.temp_alert_direction,
        )
        if event in ("breach_high", "breach_low"):
            can_send = (
                state.last_temp_alert_at is None or now - state.last_temp_alert_at > cooldown
            )
            if can_send:
                word = "สูงเกินกำหนด" if event == "breach_high" else "ต่ำกว่ากำหนด"
                limit = settings.temp_high if event == "breach_high" else settings.temp_low
                sign = ">" if event == "breach_high" else "<"
                msg = f"🌡️ {device_id}: อุณหภูมิ {temperature}°C {word} ({sign} {limit}°C){link_suffix}"
                if send_telegram_message(settings.telegram_chat_id, msg):
                    state.last_temp_alert_at = now
                    state.temp_alert_active = new_active
                    state.temp_alert_direction = new_direction
        elif event == "clear":
            msg = f"✅ {device_id}: อุณหภูมิกลับสู่ระดับปกติแล้ว ({temperature}°C)"
            send_telegram_message(settings.telegram_chat_id, msg)
            state.temp_alert_active = new_active
            state.temp_alert_direction = new_direction
        else:
            state.temp_alert_active = new_active
            state.temp_alert_direction = new_direction

    if humidity is not None:
        new_active, new_direction, event = _evaluate_metric(
            humidity,
            settings.humid_high,
            settings.humid_high_clear,
            settings.humid_low,
            settings.humid_low_clear,
            state.humid_alert_active,
            state.humid_alert_direction,
        )
        if event in ("breach_high", "breach_low"):
            can_send = (
                state.last_humid_alert_at is None or now - state.last_humid_alert_at > cooldown
            )
            if can_send:
                word = "สูงเกินกำหนด" if event == "breach_high" else "ต่ำกว่ากำหนด"
                limit = settings.humid_high if event == "breach_high" else settings.humid_low
                sign = ">" if event == "breach_high" else "<"
                msg = f"💧 {device_id}: ความชื้น {humidity}% {word} ({sign} {limit}%){link_suffix}"
                if send_telegram_message(settings.telegram_chat_id, msg):
                    state.last_humid_alert_at = now
                    state.humid_alert_active = new_active
                    state.humid_alert_direction = new_direction
        elif event == "clear":
            msg = f"✅ {device_id}: ความชื้นกลับสู่ระดับปกติแล้ว ({humidity}%)"
            send_telegram_message(settings.telegram_chat_id, msg)
            state.humid_alert_active = new_active
            state.humid_alert_direction = new_direction
        else:
            state.humid_alert_active = new_active
            state.humid_alert_direction = new_direction

    db.commit()
