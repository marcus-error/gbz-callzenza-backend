from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class PermissionOut(BaseModel):
    id: str
    name: str
    description: str = ""
    module: str = "GENERAL"

    model_config = {"from_attributes": True}


class RoleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=255)
    permission_ids: List[str] = Field(default_factory=list)


class RoleUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    permission_ids: Optional[List[str]] = None


class RoleOut(BaseModel):
    id: str
    tenant_id: Optional[str] = None
    name: str
    description: str = ""
    is_system: bool = False
    permissions: List[PermissionOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
