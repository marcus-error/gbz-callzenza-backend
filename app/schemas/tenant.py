from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class TenantBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    slug: str = Field(..., min_length=1, max_length=100)
    subdomain: Optional[str] = Field(None, max_length=100)
    custom_domain: Optional[str] = Field(None, max_length=255)
    cluster_id: Optional[str] = Field("default", max_length=50)
    server_url: Optional[str] = Field(None, max_length=255)
    region: Optional[str] = Field("us-east", max_length=50)
    isolation_strategy: Optional[str] = Field("SHARED_DATABASE", max_length=50)
    database_url: Optional[str] = Field(None, max_length=500)
    schema_name: Optional[str] = Field(None, max_length=100)


class TenantCreate(TenantBase):
    pass


class TenantUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    slug: Optional[str] = Field(None, min_length=1, max_length=100)
    subdomain: Optional[str] = Field(None, max_length=100)
    custom_domain: Optional[str] = Field(None, max_length=255)
    status: Optional[str] = Field(None, pattern="^(active|suspended|deleted)$")
    cluster_id: Optional[str] = Field(None, max_length=50)
    server_url: Optional[str] = Field(None, max_length=255)
    region: Optional[str] = Field(None, max_length=50)
    isolation_strategy: Optional[str] = Field(None, max_length=50)
    database_url: Optional[str] = Field(None, max_length=500)
    schema_name: Optional[str] = Field(None, max_length=100)


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    subdomain: Optional[str] = None
    custom_domain: Optional[str] = None
    status: str
    cluster_id: Optional[str] = "default"
    server_url: Optional[str] = None
    region: Optional[str] = "us-east"
    isolation_strategy: Optional[str] = "SHARED_DATABASE"
    database_url: Optional[str] = None
    schema_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TenantRoutingInfo(BaseModel):
    tenant_id: str
    name: str
    slug: str
    subdomain: str
    custom_domain: Optional[str] = None
    cluster_id: str
    server_url: Optional[str] = None
    region: str
    isolation_strategy: str
    is_local_cluster: bool

