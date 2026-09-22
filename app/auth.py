import os
from datetime import datetime, timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from .database import get_db, User

SECRET_KEY = os.getenv("JWT_SECRET", "change-me-please")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("JWT_EXPIRE_MINUTES", "480"))

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/iot/api/login")


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


DEVICE_VIEW_LINK_HOURS = int(os.getenv("DEVICE_VIEW_LINK_HOURS", "24"))


def create_access_token(data: dict, expires_minutes: int = ACCESS_TOKEN_EXPIRE_MINUTES) -> str:
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=expires_minutes)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_device_view_token(device_id: str, expire_hours: int = DEVICE_VIEW_LINK_HOURS) -> str:
    """Short-lived, read-only, single-device token embedded in Telegram
    alert links. Tapping the link opens straight to that device's graph
    with no login prompt - but this token can ONLY be used to read that one
    device's sensor data (see get_principal below). It cannot change any
    setting, see any other device, or do anything an admin/owner token can,
    and it stops working after expire_hours."""
    return create_access_token(
        {"scope": "device_view", "device_id": device_id},
        expires_minutes=expire_hours * 60,
    )


def authenticate_user(db: Session, username: str, password: str):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.hashed_password):
        return None
    return user


async def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user


async def get_principal(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    """Like get_current_user, but also accepts a device-view token from an
    alert link. Returns either a full User, or {"device_view": device_id}
    for a scoped, read-only alert-link token. Only use this where
    device-scoped read access is genuinely safe to grant (sensor data for
    that one device) - never for settings or admin endpoints, which stay on
    get_current_user / require_admin so a leaked alert link can't do more
    than view that one device's graph."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise credentials_exception

    if payload.get("scope") == "device_view":
        device_id = payload.get("device_id")
        if not device_id:
            raise credentials_exception
        return {"device_view": device_id}

    username = payload.get("sub")
    if username is None:
        raise credentials_exception
    user = db.query(User).filter(User.username == username).first()
    if user is None:
        raise credentials_exception
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return user
