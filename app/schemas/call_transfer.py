from __future__ import annotations

from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


class IntentCheckRequest(BaseModel):
    text: str = Field(..., description="Customer utterance text to analyze for transfer intent")


class IntentCheckResponse(BaseModel):
    has_intent: bool
    confidence: float
    language: str
    matched_phrase: Optional[str] = None
    requires_confirmation: bool = False
    reason: str = ""


class CallTransferInitiateRequest(BaseModel):
    call_id: str
    session_id: Optional[str] = None
    campaign_id: Optional[str] = None
    lead_id: Optional[str] = None
    customer_name: Optional[str] = "Customer"
    customer_phone: Optional[str] = ""
    language: Optional[str] = "en"
    notes: Optional[str] = ""
    transcript: Optional[str] = ""
    target_agent_id: Optional[str] = None


class CallTransferResponse(BaseModel):
    transfer_id: str
    call_id: str
    status: str
    assigned_agent_id: Optional[str] = None
    assigned_agent_name: Optional[str] = None
    assigned_agent_phone: Optional[str] = None
    message: str
    is_fallback: bool = False
    details: Optional[Dict[str, Any]] = None


class CallTransferAcceptRequest(BaseModel):
    agent_id: Optional[str] = None
    notes: Optional[str] = None


class CallTransferDeclineRequest(BaseModel):
    agent_id: Optional[str] = None
    reason: Optional[str] = "Voice agent declined transfer"


class AgentPresenceUpdateRequest(BaseModel):
    agent_id: str
    status: str = Field(..., description="AVAILABLE, RINGING, ON_CALL, BUSY, OFFLINE, BREAK")


class AgentPresenceItem(BaseModel):
    agent_id: str
    agent_name: str
    agent_email: Optional[str] = ""
    status: str
    is_available: bool
    skills: List[str] = []
    current_call_id: Optional[str] = None
    last_status_change: Optional[str] = None


class AgentPresenceListResponse(BaseModel):
    agents: List[AgentPresenceItem]
    total_available: int
    total_agents: int


class TransferStatusDetailResponse(BaseModel):
    transfer_id: str
    call_id: str
    status: str
    customer_name: str
    customer_phone: str
    assigned_voice_agent_id: Optional[str] = None
    assigned_voice_agent_name: Optional[str] = None
    intent_detected_at: Optional[str] = None
    ringing_at: Optional[str] = None
    accepted_at: Optional[str] = None
    ai_disconnected_at: Optional[str] = None
    completed_at: Optional[str] = None
    failure_reason: Optional[str] = None
    transcript_snapshot: Optional[str] = None
