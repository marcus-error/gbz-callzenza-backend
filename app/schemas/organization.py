from datetime import datetime
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field


class OrganizationBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    org_type: str = Field("COMPANY", pattern="^(COMPANY|BRANCH|DEPARTMENT)$")
    code: Optional[str] = None
    parent_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field("active", pattern="^(active|inactive)$")


class OrganizationCreate(OrganizationBase):
    pass


class OrganizationUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    org_type: Optional[str] = Field(None, pattern="^(COMPANY|BRANCH|DEPARTMENT)$")
    code: Optional[str] = None
    parent_id: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    status: Optional[str] = Field(None, pattern="^(active|inactive)$")


class OrganizationOut(OrganizationBase):
    id: str
    tenant_id: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
