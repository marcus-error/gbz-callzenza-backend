from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator


class DemoRequestCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    organization_name: str = Field(..., alias="organizationName", min_length=1, max_length=255)
    email: EmailStr = Field(...)

    @field_validator("organization_name")
    @classmethod
    def sanitize_organization(cls, v: str) -> str:
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Organization Name cannot be empty")
        return trimmed

    @field_validator("email")
    @classmethod
    def sanitize_email(cls, v: str) -> str:
        trimmed = v.strip().lower()
        if not trimmed:
            raise ValueError("Email Address cannot be empty")
        return trimmed


class DemoRequestResponse(BaseModel):
    success: bool = True
    message: str = "Demo request submitted successfully"
    demo_request_id: Optional[str] = None


class DemoRequestItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_name: str
    email: str
    status: str
    created_at: datetime
    updated_at: datetime


class DemoRequestStatusUpdate(BaseModel):
    status: str = Field(..., min_length=1, max_length=50)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        allowed = {"NEW", "CONTACTED", "SCHEDULED", "COMPLETED", "ARCHIVED"}
        upper_v = v.strip().upper()
        if upper_v not in allowed:
            raise ValueError(f"Status must be one of {', '.join(allowed)}")
        return upper_v


class SuperAdminNotificationItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    business_id: Optional[str] = None
    user_id: str
    title: str
    message: str
    type: str
    reference_id: Optional[str] = None
    read: bool
    created_at: datetime
    demo_request: Optional[DemoRequestItem] = None
