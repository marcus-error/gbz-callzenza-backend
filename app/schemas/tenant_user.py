from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, EmailStr, Field


class TenantUserCreate(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8)
    full_name: str = Field("", max_length=255)
    role: str = Field("AGENT", max_length=50)
    permissions: Optional[Dict[str, Any]] = None


class TenantUserUpdate(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    permissions: Optional[Dict[str, Any]] = None


class TenantUserInvite(BaseModel):
    email: EmailStr
    full_name: str = ""
    role: str = "AGENT"


class TenantUserOut(BaseModel):
    id: str
    email: str
    full_name: str = ""
    role: str = "AGENT"
    business_id: Optional[str] = None
    is_active: bool = True
    permissions: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
