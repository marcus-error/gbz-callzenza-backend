from __future__ import annotations

from typing import List, Optional, Any, Dict
from pydantic import BaseModel, EmailStr, Field


# ---------------------------------------------------------------------------
# Auth & User Schemas
# ---------------------------------------------------------------------------

class PortalLoginRequest(BaseModel):
    email: str = Field(..., description="Email address or username of the portal user")
    password: str = Field(..., description="Account password")


class PortalUserInfo(BaseModel):
    id: str
    email: str
    full_name: str
    role: str = "AI_AGENT_PORTAL_USER"
    business_id: Optional[str] = None
    access_scope: str = "ALL_AGENTS"  # ALL_AGENTS or SELECTED_AGENTS
    assigned_agent_ids: List[str] = Field(default_factory=list)
    is_active: bool = True
    created_at: Optional[str] = None


class PortalLoginResponse(BaseModel):
    access_token: str
    business_id: Optional[str] = None
    tenant_id: Optional[str] = None
    role: str = "AI_AGENT_PORTAL_USER"
    token_type: str = "bearer"
    user: PortalUserInfo


class CreatePortalUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(..., min_length=1)
    password: str = Field(..., min_length=6)
    access_scope: str = Field("ALL_AGENTS", description="ALL_AGENTS or SELECTED_AGENTS")
    agent_ids: Optional[List[str]] = Field(default_factory=list)


class UpdatePortalUserRequest(BaseModel):
    name: Optional[str] = None
    password: Optional[str] = None
    is_active: Optional[bool] = None
    access_scope: Optional[str] = None
    agent_ids: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# Agent List Schemas
# ---------------------------------------------------------------------------

class PortalAgentItem(BaseModel):
    id: str
    name: str
    type: str  # AI Agent / Voice Agent
    status: str
    owner: str
    source: str
    phone_number: str
    total_calls: int = 0
    last_activity: Optional[str] = None
    created_at: Optional[str] = None


class PortalAgentsPageResponse(BaseModel):
    items: List[PortalAgentItem]
    page: int
    page_size: int
    total: int
    total_pages: int


# ---------------------------------------------------------------------------
# End-to-End Tab Schemas
# ---------------------------------------------------------------------------

class PortalBlacklistItem(BaseModel):
    id: str
    phone: str
    name: str
    status: str
    reason: Optional[str] = None
    source: Optional[str] = None
    added_at: Optional[str] = None


class PortalBlacklistResponse(BaseModel):
    items: List[PortalBlacklistItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    available: bool = True


class PortalVoiceItem(BaseModel):
    call_id: str
    customer_name: str
    customer_phone: str
    agent_name: Optional[str] = None
    agent_id: Optional[str] = None
    transfer_status: str
    direction: str = "OUTBOUND"
    duration_seconds: int = 0
    initiated_at: Optional[str] = None
    recording_url: Optional[str] = None


class PortalVoiceResponse(BaseModel):
    items: List[PortalVoiceItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    available: bool = True
    reason: Optional[str] = None


class PortalQualifiedLeadItem(BaseModel):
    lead_id: str
    name: str
    phone: str
    company: Optional[str] = None
    qualification_status: str
    score: int = 0
    ai_summary: Optional[str] = None
    qualified_at: Optional[str] = None


class PortalQualifiedResponse(BaseModel):
    items: List[PortalQualifiedLeadItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    available: bool = True


class PortalCompletedCallItem(BaseModel):
    call_id: str
    destination: str
    customer_name: str
    duration_seconds: int = 0
    duration_formatted: str = "00:00"
    status: str = "COMPLETED"
    outcome: Optional[str] = None
    sentiment: Optional[str] = None
    recording_url: Optional[str] = None
    completed_at: Optional[str] = None


class PortalCompletedResponse(BaseModel):
    items: List[PortalCompletedCallItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    available: bool = True


class PortalRecordingItem(BaseModel):
    call_id: str
    customer_name: str
    phone_number: str
    duration_seconds: int = 0
    duration_formatted: str = "00:00"
    recording_url: str
    status: str
    created_at: Optional[str] = None


class PortalRecordingsResponse(BaseModel):
    items: List[PortalRecordingItem]
    page: int
    page_size: int
    total: int
    total_pages: int
    available: bool = True
