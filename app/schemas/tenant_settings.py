from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class TenantSettingsUpdate(BaseModel):
    timezone: Optional[str] = None
    currency: Optional[str] = None
    date_format: Optional[str] = None
    calling_hours_start: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    calling_hours_end: Optional[str] = Field(None, pattern=r"^\d{2}:\d{2}$")
    retry_limit: Optional[int] = Field(None, ge=0, le=10)
    retry_interval_minutes: Optional[int] = Field(None, ge=1, le=1440)
    voicemail_detection: Optional[bool] = None
    custom_flags: Optional[Dict[str, Any]] = None


class TenantSettingsOut(BaseModel):
    id: str
    tenant_id: str
    timezone: str = "UTC"
    currency: str = "USD"
    date_format: str = "YYYY-MM-DD"
    calling_hours_start: str = "09:00"
    calling_hours_end: str = "18:00"
    retry_limit: int = 3
    retry_interval_minutes: int = 60
    voicemail_detection: bool = True
    custom_flags: Dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
