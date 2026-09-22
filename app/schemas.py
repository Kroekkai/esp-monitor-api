from typing import Optional

from pydantic import BaseModel


class SensorData(BaseModel):
    device_id: str
    temperature: float
    humidity: float


class LoginRequest(BaseModel):
    username: str
    password: str


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
