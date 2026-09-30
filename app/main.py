import csv
import io
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .alerts import check_and_alert, device_link, get_settings, send_telegram_message, update_settings
from .telegram_bot import WEBHOOK_SECRET, create_link_code, handle_incoming_update, register_webhook
from .auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_principal,
    hash_password,
    oauth2_scheme_optional,
    require_admin,
    resolve_jwt_principal,
    verify_password,
)
from .database import Device, ShortLink, User, get_db, init_db
from .influx import query_latest, query_recent, write_sensor_point
from .schemas import (
    AlertSettingsIn,
    AlertSettingsOut,
    ChangePasswordRequest,
    DeviceIn,
    LoginRequest,
    RegisterRequest,
    SensorData,
    Token,
    UserCreate,
    UserOut,
)

app = FastAPI(title="ESP Temperature/Humidity Monitor")
logger = logging.getLogger("esp-monitor")

# RFC3339 UTC timestamp, e.g. "2026-09-15T00:00:00Z" - used to validate the
# calendar "pick a day" start/stop query params before they ever reach the
# Flux query string.
ISO_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

DEVICE_API_KEY = os.getenv("DEVICE_API_KEY", "")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

# Everything (API + web page) lives under /iot so it can share a host/reverse
# proxy with other services without clashing on paths.
iot = APIRouter(prefix="/iot")


async def get_principal_or_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    header_token: Optional[str] = Depends(oauth2_scheme_optional),
    token: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """For read-only sensor endpoints that another device (not a person)
    might poll: accepts the SAME X-API-Key already trusted for writing
    sensor data (POST /api/sensor) as an alternative to a user login/JWT -
    no token, no expiry, no refresh logic needed on the device side. A
    valid key grants the same "see everything" access an admin JWT would
    (the key is already trusted to write any device's data, so trusting it
    to read any device's data is no more permissive). Falls back to the
    normal get_principal resolution (JWT via header or ?token=) when no
    X-API-Key header is present at all."""
    if x_api_key:
        if not DEVICE_API_KEY or x_api_key != DEVICE_API_KEY:
            raise HTTPException(status_code=401, detail="Invalid device API key")
        return "device_api_key"

    actual_token = header_token or token
    if not actual_token:
        raise HTTPException(status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"})
    return resolve_jwt_principal(actual_token, db)


@app.on_event("startup")
def on_startup():
    init_db()
    from .database import SessionLocal

    db = SessionLocal()
    try:
        if not db.query(User).filter(User.username == "admin").first():
            admin_pw = os.getenv("DEFAULT_ADMIN_PASSWORD", "changeme123")
            db.add(
                User(
                    username="admin",
                    hashed_password=hash_password(admin_pw),
                    role="admin",
                )
            )
            db.commit()
    finally:
        db.close()

    # Best-effort: register our webhook with Telegram so /start <code>
    # deep-link messages reach us automatically. Silently does nothing if
    # TELEGRAM_BOT_TOKEN or APP_PUBLIC_URL aren't set - Telegram features
    # that need them (alerts, auto-link) just won't work until they are,
    # same as before this feature existed.
    if register_webhook():
        logger.info("Telegram webhook registered")


@app.get("/health")
def health():
    return {"status": "ok"}


def _owned_device_ids(db: Session, user: User) -> list:
    return [
        d.device_id
        for d in db.query(Device).filter(Device.owner_user_id == user.id).all()
    ]


def _require_device_access(db: Session, principal, device_id: str) -> None:
    """Admin can touch any device. A regular user only their own. A
    device-view alert-link token only the single device it was scoped to at
    creation - it can read AND edit that one device's alert settings (so
    tapping a Telegram alert lets you adjust the threshold on the spot) but
    can never reach any other device, user, or admin action."""
    if isinstance(principal, dict):
        if principal.get("device_view") != device_id:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")
        return
    user = principal
    if user.role == "admin":
        return
    owned = db.query(Device).filter(
        Device.device_id == device_id, Device.owner_user_id == user.id
    ).first()
    if not owned:
        raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")


# ---------------------------------------------------------------------------
# Web dashboard (static page) - served at /iot and /iot/
# ---------------------------------------------------------------------------
@iot.get("/")
def dashboard():
    # Never cache index.html itself - it's what carries the ?v= cache-busting
    # query on app.js/style.css, so a stale cached index.html would keep
    # pointing at stale (possibly broken) cached JS/CSS forever.
    return FileResponse(
        os.path.join(STATIC_DIR, "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@iot.get("/s/{short_id}")
def resolve_short_link(short_id: str, db: Session = Depends(get_db)):
    """Redirect target for the short links handed out in Telegram alert
    messages (see alerts.device_link) - keeps the message text short
    instead of pasting the full URL with its long JWT view-token in it."""
    link = db.query(ShortLink).filter(ShortLink.short_id == short_id).first()
    if not link or link.expires_at < datetime.utcnow():
        raise HTTPException(status_code=404, detail="ลิงก์นี้หมดอายุหรือไม่ถูกต้อง")
    return RedirectResponse(url=link.target_url)


# ---------------------------------------------------------------------------
# Single POST endpoint used by the ESP32/ESP8266 devices.
# Sensor reading arrives here and is written straight into InfluxDB inside
# the same request handler (no separate microservice hop). Any device_id can
# post - it doesn't need to be pre-registered - but only devices an admin has
# assigned to a user will show up in that user's dashboard/alerts.
# ---------------------------------------------------------------------------
@iot.post("/api/sensor")
def post_sensor_data(
    data: SensorData,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
):
    if DEVICE_API_KEY and x_api_key != DEVICE_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid device API key")
    write_sensor_point(data.device_id, data.temperature, data.humidity)
    try:
        check_and_alert(db, data.device_id, data.temperature, data.humidity)
    except Exception:
        # Telegram/alert logic must never break sensor ingestion.
        logger.exception("alert check failed for device_id=%s", data.device_id)
    return {"status": "written", "device_id": data.device_id}


@iot.post("/api/login", response_model=Token)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = authenticate_user(db, payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_access_token({"sub": user.username, "role": user.role})
    return Token(access_token=token)


@iot.post("/api/register", response_model=Token)
def register(payload: RegisterRequest, db: Session = Depends(get_db)):
    """Self-service signup - open to anyone, no login required to call this.
    Always creates a plain "user" account (RegisterRequest has no role
    field at all - see schemas.py) with zero devices assigned; an admin
    still has to assign a device before the new account sees any data.
    Logs the new user straight in (same as a successful /api/login) so
    they land on the dashboard immediately instead of registering then
    having to log in separately."""
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    user = User(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        role="user",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token({"sub": user.username, "role": user.role})
    return Token(access_token=token)


@iot.post("/api/users/me/password")
def change_own_password(
    payload: ChangePasswordRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Any logged-in real user (admin-created or self-registered - no
    difference once the account exists) can change their own password,
    as long as they can prove they know the current one. Deliberately uses
    get_current_user (not get_principal), so a device-view Telegram-link
    token can never reach this - those aren't real accounts."""
    if not verify_password(payload.current_password, user.hashed_password):
        raise HTTPException(status_code=401, detail="รหัสผ่านเดิมไม่ถูกต้อง")
    user.hashed_password = hash_password(payload.new_password)
    db.commit()
    return {"status": "changed"}


@iot.get("/api/sensor/data")
def get_sensor_data(
    hours: int = 24,
    device_id: Optional[str] = None,
    start: Optional[str] = None,
    stop: Optional[str] = None,
    db: Session = Depends(get_db),
    principal=Depends(get_principal_or_api_key),
):
    # Calendar "pick a day" mode: both start and stop must be well-formed
    # RFC3339 UTC timestamps (e.g. "2026-09-15T00:00:00Z") - the frontend
    # computes these from the chosen local date so the day boundary matches
    # the viewer's own timezone, not the server's. Reject anything else
    # before it ever reaches the Flux query string.
    date_range = {}
    if start or stop:
        if not (start and stop and ISO_TIMESTAMP_RE.match(start) and ISO_TIMESTAMP_RE.match(stop)):
            raise HTTPException(status_code=400, detail="รูปแบบวันที่ไม่ถูกต้อง")
        date_range = {"start": start, "stop": stop}

    # The shared device API key - already trusted to write any device's
    # data - can read any device's data too, same as an admin.
    if principal == "device_api_key":
        return query_recent(hours=hours, device_id=device_id, **date_range)

    # A device-view token (from a Telegram alert link) can only ever see
    # the one device it was scoped to at creation time.
    if isinstance(principal, dict):
        allowed_device = principal["device_view"]
        if device_id and device_id != allowed_device:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")
        return query_recent(hours=hours, device_id=allowed_device, **date_range)

    user = principal
    if user.role == "admin":
        return query_recent(hours=hours, device_id=device_id, **date_range)

    owned = _owned_device_ids(db, user)
    if device_id:
        if device_id not in owned:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")
        return query_recent(hours=hours, device_id=device_id, **date_range)
    return query_recent(hours=hours, device_ids=owned, **date_range)


@iot.get("/api/sensor/latest")
def get_sensor_latest(
    device_id: Optional[str] = None,
    db: Session = Depends(get_db),
    principal=Depends(get_principal_or_api_key),
):
    """Simple export endpoint for external integrations: just the latest
    reading per device, three fields only (device_id, temperature,
    humidity) - no timestamp, no history. Same access rules as
    /api/sensor/data (device-view token = that one device only, regular
    user = only their own devices, admin/device API key = everything)."""
    single_device = device_id  # remember if the caller pinned one device

    if principal == "device_api_key":
        rows = query_latest(device_id=device_id) if device_id else query_latest()
    elif isinstance(principal, dict):
        allowed_device = principal["device_view"]
        if device_id and device_id != allowed_device:
            raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")
        # A device-view token is always scoped to exactly one device, so the
        # response is a single object even if ?device_id= wasn't passed.
        single_device = allowed_device
        rows = query_latest(device_id=allowed_device)
    else:
        user = principal
        if user.role == "admin":
            rows = query_latest(device_id=device_id) if device_id else query_latest()
        else:
            owned = _owned_device_ids(db, user)
            if device_id:
                if device_id not in owned:
                    raise HTTPException(status_code=403, detail="ไม่มีสิทธิ์เข้าถึงอุปกรณ์นี้")
                rows = query_latest(device_id=device_id)
            else:
                rows = query_latest(device_ids=owned)

    # A specific device was asked for (or implied by a device-view token) ->
    # return a single flat object, not a one-item list.
    if single_device:
        return rows[0] if rows else {"device_id": single_device, "temperature": None, "humidity": None}
    return rows


# ---------------------------------------------------------------------------
# Devices - admin manages which device belongs to which user. A regular user
# can only list their own devices.
# ---------------------------------------------------------------------------
@iot.get("/api/devices/mine")
def list_my_devices(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "admin":
        devices = db.query(Device).all()
    else:
        devices = db.query(Device).filter(Device.owner_user_id == user.id).all()
    return [{"device_id": d.device_id, "name": d.name} for d in devices]


@iot.get("/api/devices")
def list_devices(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    devices = db.query(Device).all()
    users_by_id = {u.id: u.username for u in db.query(User).all()}
    return [
        {
            "device_id": d.device_id,
            "name": d.name,
            "owner_username": users_by_id.get(d.owner_user_id),
        }
        for d in devices
    ]


@iot.post("/api/devices")
def create_or_assign_device(
    payload: DeviceIn,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    owner_id = None
    if payload.owner_username:
        owner = db.query(User).filter(User.username == payload.owner_username).first()
        if not owner:
            raise HTTPException(status_code=400, detail="ไม่พบ user ชื่อนี้")
        owner_id = owner.id

    device = db.query(Device).filter(Device.device_id == payload.device_id).first()
    if device:
        device.name = payload.name
        device.owner_user_id = owner_id
    else:
        device = Device(device_id=payload.device_id, name=payload.name, owner_user_id=owner_id)
        db.add(device)
    db.commit()
    return {"status": "saved", "device_id": payload.device_id}


@iot.delete("/api/devices/{device_id}")
def delete_device(
    device_id: str,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    device = db.query(Device).filter(Device.device_id == device_id).first()
    if not device:
        raise HTTPException(status_code=404, detail="ไม่พบอุปกรณ์นี้")
    db.delete(device)
    db.commit()
    return {"status": "deleted"}


@iot.post("/api/users", response_model=UserOut)
def create_user(
    payload: UserCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username already exists")
    user = User(
        username=payload.username,
        hashed_password=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@iot.get("/api/users")
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return [{"username": u.username, "role": u.role} for u in db.query(User).all()]


@iot.get("/api/users/export")
def export_users_csv(
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    users = db.query(User).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "username", "role", "created_at"])
    for u in users:
        writer.writerow([u.id, u.username, u.role, u.created_at])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=users.csv"},
    )


# ---------------------------------------------------------------------------
# Alert settings (Telegram) - per device. Admin can edit any device; a
# regular user only the device(s) they own. The bot token itself stays in
# .env since it's a secret shared across the whole app.
# ---------------------------------------------------------------------------
@iot.get("/api/settings/alerts/{device_id}", response_model=AlertSettingsOut)
def read_alert_settings(
    device_id: str,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
):
    _require_device_access(db, principal, device_id)
    return get_settings(db, device_id)


@iot.post("/api/settings/alerts/{device_id}", response_model=AlertSettingsOut)
def write_alert_settings(
    device_id: str,
    payload: AlertSettingsIn,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
):
    _require_device_access(db, principal, device_id)
    return update_settings(db, device_id, payload)


@iot.post("/api/settings/alerts/{device_id}/test")
def test_alert_settings(
    device_id: str,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
):
    _require_device_access(db, principal, device_id)
    settings = get_settings(db, device_id)
    if not settings.telegram_chat_id:
        raise HTTPException(status_code=400, detail="ยังไม่ได้ตั้งค่า Telegram Chat ID")
    link = device_link(db, device_id)
    link_suffix = f"\n{link}" if link else ""
    ok = send_telegram_message(
        settings.telegram_chat_id,
        f"🔔 ทดสอบการแจ้งเตือนจาก ESP Monitor ({device_id}){link_suffix}",
    )
    if not ok:
        raise HTTPException(
            status_code=502,
            detail="ส่งข้อความไม่สำเร็จ ตรวจสอบ TELEGRAM_BOT_TOKEN ใน .env และ Chat ID ให้ถูกต้อง",
        )
    return {"status": "sent"}


@iot.post("/api/settings/alerts/{device_id}/telegram/link-code")
def create_telegram_link_code(
    device_id: str,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
):
    """Generates a one-time code + t.me deep link for connecting this
    device's alerts to a Telegram chat, without anyone having to look up
    or type in a numeric chat_id by hand. Expires after a few minutes."""
    _require_device_access(db, principal, device_id)
    result = create_link_code(db, device_id)
    if not result["bot_link"]:
        raise HTTPException(
            status_code=502,
            detail="ไม่สามารถติดต่อ Telegram ได้ ตรวจสอบ TELEGRAM_BOT_TOKEN ใน .env",
        )
    return result


@iot.post("/api/settings/alerts/{device_id}/telegram/disconnect")
def disconnect_telegram(
    device_id: str,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
):
    """Permanently unlinks this device's Telegram chat (clears
    telegram_chat_id only) - every other setting (thresholds, cooldown,
    enabled flag) is left untouched, and reconnecting later is just the
    normal link-code flow again."""
    _require_device_access(db, principal, device_id)
    settings = get_settings(db, device_id)
    settings.telegram_chat_id = None
    db.commit()
    return {"status": "disconnected"}


@iot.post("/api/telegram/webhook")
async def telegram_webhook(
    request: Request,
    db: Session = Depends(get_db),
    x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
):
    """Public endpoint Telegram itself calls whenever the bot receives a
    message (registered via register_webhook() at startup). Verifies the
    secret token Telegram echoes back on every call (set during
    setWebhook) so a random POST from someone who finds this URL can't
    pretend to be a Telegram update. Always returns 200 quickly - Telegram
    disables a webhook that errors or times out too often."""
    if not WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")
    update = await request.json()
    handle_incoming_update(db, update)
    return {"ok": True}


app.include_router(iot)
# Static assets (css/js) for the dashboard, served at /iot/static/...
app.mount("/iot/static", StaticFiles(directory=STATIC_DIR), name="iot-static")
