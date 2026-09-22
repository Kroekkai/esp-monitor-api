import os
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, String, create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

SQLITE_PATH = os.getenv("SQLITE_PATH", "./data/app.db")
os.makedirs(os.path.dirname(SQLITE_PATH) or ".", exist_ok=True)

engine = create_engine(
    f"sqlite:///{SQLITE_PATH}",
    connect_args={"check_same_thread": False},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    role = Column(String, default="user")
    created_at = Column(DateTime, default=datetime.utcnow)


class Device(Base):
    """A physical ESP device. Admin creates/assigns these; a regular user
    can only see and configure the devices they own."""

    __tablename__ = "devices"

    device_id = Column(String, primary_key=True)
    name = Column(String, nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class AlertSettings(Base):
    """One row per device_id holding that device's Telegram alert thresholds.
    (Table name is new - device_alert_settings - so upgrading from the old
    single global-settings build doesn't collide with its old schema.)

    Hysteresis: *_clear thresholds are optional "must come back past this
    point" values, separate from the breach thresholds, so a value flapping
    right at the breach line doesn't trigger repeated alerts. If a *_clear
    value is left unset, it falls back to the breach threshold itself
    (old behaviour, no hysteresis)."""

    __tablename__ = "device_alert_settings"

    device_id = Column(String, primary_key=True)
    temp_high = Column(Float, nullable=True)
    temp_high_clear = Column(Float, nullable=True)
    temp_low = Column(Float, nullable=True)
    temp_low_clear = Column(Float, nullable=True)
    humid_high = Column(Float, nullable=True)
    humid_high_clear = Column(Float, nullable=True)
    humid_low = Column(Float, nullable=True)
    humid_low_clear = Column(Float, nullable=True)
    telegram_chat_id = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)
    cooldown_minutes = Column(Integer, default=15)


class AlertState(Base):
    """Per-device edge-trigger state so we alert once per breach, plus a
    'back to normal' message, instead of spamming on every sensor POST.
    *_alert_direction remembers whether the active alarm was a "high" or
    "low" breach, since with hysteresis the clear threshold differs by
    direction."""

    __tablename__ = "alert_state"

    device_id = Column(String, primary_key=True)
    temp_alert_active = Column(Boolean, default=False)
    temp_alert_direction = Column(String, nullable=True)  # "high" | "low" | None
    humid_alert_active = Column(Boolean, default=False)
    humid_alert_direction = Column(String, nullable=True)
    last_temp_alert_at = Column(DateTime, nullable=True)
    last_humid_alert_at = Column(DateTime, nullable=True)


class ShortLink(Base):
    """Self-hosted URL shortener for Telegram alert links - the full link
    (with its device-view JWT) is long, so we hand out a short random id
    that redirects to it instead. Expires alongside the token it points to
    so a stale short link can't outlive the access it was meant to grant."""

    __tablename__ = "short_links"

    short_id = Column(String, primary_key=True)
    target_url = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False)


def init_db():
    Base.metadata.create_all(bind=engine)
    _ensure_columns(
        "device_alert_settings",
        [
            ("temp_high_clear", "FLOAT"),
            ("temp_low_clear", "FLOAT"),
            ("humid_high_clear", "FLOAT"),
            ("humid_low_clear", "FLOAT"),
        ],
    )
    _ensure_columns(
        "alert_state",
        [
            ("temp_alert_direction", "VARCHAR"),
            ("humid_alert_direction", "VARCHAR"),
        ],
    )


def _ensure_columns(table: str, columns: list) -> None:
    """Lightweight auto-migration: add any columns that are new in the model
    but missing from an existing table, so upgrading doesn't wipe data.
    No-op for a brand-new database (create_all already made the table with
    every column)."""
    with engine.connect() as conn:
        existing = {
            row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")
        }
        for name, sql_type in columns:
            if name not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
        conn.commit()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
