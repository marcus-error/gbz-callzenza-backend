from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class NotificationCreate(BaseModel):
    channel: str = Field(..., pattern="^(EMAIL|SMS|WHATSAPP)$")
    recipient: str = Field(..., min_length=1)
    subject: Optional[str] = None
    content: str = Field(..., min_length=1)
    notification_metadata: Optional[Dict[str, Any]] = None


class NotificationOut(BaseModel):
    id: str
    tenant_id: str
    channel: str
    recipient: str
    subject: Optional[str] = None
    content: str
    status: str
    external_id: Optional[str] = None
    error: Optional[str] = None
    notification_metadata: Optional[Dict[str, Any]] = None
    sent_at: Optional[datetime] = None
    created_at: datetime

    model_config = {"from_attributes": True}
