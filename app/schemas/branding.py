from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class TenantBrandingUpdate(BaseModel):
    company_name: Optional[str] = None
    logo_url: Optional[str] = None
    dark_logo_url: Optional[str] = None
    favicon_url: Optional[str] = None
    primary_color: Optional[str] = Field(None, max_length=30)
    secondary_color: Optional[str] = Field(None, max_length=30)
    accent_color: Optional[str] = Field(None, max_length=30)
    custom_css: Optional[str] = None
    support_email: Optional[str] = None
    support_phone: Optional[str] = None
    website: Optional[str] = None


class TenantBrandingOut(BaseModel):
    id: str
    tenant_id: str
    company_name: str = ""
    logo_url: str = ""
    dark_logo_url: str = ""
    favicon_url: str = ""
    primary_color: str = "#4F46E5"
    secondary_color: str = "#06B6D4"
    accent_color: str = "#10B981"
    custom_css: Optional[str] = ""
    support_email: str = ""
    support_phone: str = ""
    website: str = ""
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
