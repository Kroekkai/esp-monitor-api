import csv
import io
import logging
import os
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from .alerts import check_and_alert, device_link, get_settings, send_telegram_message, update_settings
from .auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    get_principal,
    hash_password,
    require_admin,
)
from .database import Device, ShortLink, User, get_db, init_db
from .influx import query_recent, write_sensor_point
from .schemas import (
    AlertSettingsIn,
    AlertSettingsOut,
    DeviceIn,
    LoginRequest,
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


@iot.get("/api/sensor/data")
def get_sensor_data(
    hours: int = 24,
    device_id: Optional[str] = None,
    start: Optional[str] = None,
    stop: Optional[str] = None,
    db: Session = Depends(get_db),
    principal=Depends(get_principal),
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


app.include_router(iot)
# Static assets (css/js) for the dashboard, served at /iot/static/...
app.mount("/iot/static", StaticFiles(directory=STATIC_DIR), name="iot-static")
