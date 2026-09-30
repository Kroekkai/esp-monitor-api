from typing import Optional

from pydantic import BaseModel, Field


class SensorData(BaseModel):
    device_id: str
    temperature: float
    humidity: float


class LoginRequest(BaseModel):
    username: str
    password: str


class ChangePasswordRequest(BaseModel):
    """Any logged-in user changes their OWN password - must prove they
    know the current one first. Works the same whether the account was
    admin-created or self-registered; there's no difference once it
    exists."""

    current_password: str
    new_password: str = Field(min_length=6, max_length=200)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserCreate(BaseModel):
    username: str
    password: str
    role: Optional[str] = "user"


class UserOut(BaseModel):
    id: int
    username: str
    role: str

    class Config:
        from_attributes = True


class AlertSettingsIn(BaseModel):
    temp_high: Optional[float] = None
    temp_high_clear: Optional[float] = None
    temp_low: Optional[float] = None
    temp_low_clear: Optional[float] = None
    humid_high: Optional[float] = None
    humid_high_clear: Optional[float] = None
    humid_low: Optional[float] = None
    humid_low_clear: Optional[float] = None
    telegram_chat_id: Optional[str] = None
    enabled: bool = True
    cooldown_minutes: int = 15


class AlertSettingsOut(AlertSettingsIn):
    class Config:
        from_attributes = True


class DeviceIn(BaseModel):
    device_id: str
    name: Optional[str] = None
    owner_username: Optional[str] = None
