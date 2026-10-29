from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict


class TransferToAgentRequest(BaseModel):
    lead_id: str
    call_id: Optional[str] = None
    campaign_id: Optional[str] = None
    ai_agent_name: Optional[str] = "AI Agent"
    qualification_stage: Optional[str] = "STAGING"
    transcript: Optional[str] = ""
    notes: Optional[str] = ""
    collected_answers: Optional[Dict[str, Any]] = None
    requested_expert_name: Optional[str] = None


class TransferToAgentResponse(BaseModel):
    success: bool
    status: str  # "ASSIGNED", "QUEUED", "QUEUED_FOR_REQUESTED_AGENT", "ALREADY_ASSIGNED", "NOT_FOUND"
    message: str
    lead_id: str
    assignment_id: Optional[str] = None
    assigned_expert: Optional[Dict[str, Any]] = None
    requested_expert: Optional[Dict[str, Any]] = None
    previous_expert: Optional[Dict[str, Any]] = None
    queue_item_id: Optional[str] = None


class CompleteAICallRequest(BaseModel):
    lead_id: str
    call_id: Optional[str] = None
    qualification_status: Optional[str] = "QUALIFIED"
    summary: Optional[str] = ""
    transcript: Optional[str] = ""
    notes: Optional[str] = ""


class QueueItemOut(BaseModel):
    id: str
    business_id: str
    lead_id: str
    lead_name: str
    phone: str
    email: str
    company: str
    requested_expert_id: Optional[str] = None
    requested_expert_name: Optional[str] = None
    trigger_source: str
    reason: str
    status: str
    created_at: datetime
    assigned_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class LeadAssignmentOut(BaseModel):
    id: str
    business_id: Optional[str] = None
    lead_id: Optional[str] = None
    expert_id: Optional[str] = None
    expert_name: Optional[str] = None
    expert_email: Optional[str] = None
    previous_expert_id: Optional[str] = None
    previous_expert_name: Optional[str] = None
    strategy: Optional[str] = None
    status: Optional[str] = None
    trigger_source: Optional[str] = None
    ai_agent_name: Optional[str] = None
    qualification_stage: Optional[str] = None
    transcript: Optional[str] = None
    notes: Optional[str] = None
    context: Dict[str, Any] = {}
    assigned_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)
