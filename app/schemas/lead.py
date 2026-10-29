from datetime import datetime
from pydantic import BaseModel


class LeadCreate(BaseModel):
    source: str = "MANUAL"
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    job_title: str = ""
    requirement: str = ""
    budget: str = ""
    timeline: str = ""
    location: str = ""
    agent_id: str | None = None


class LeadUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    phone: str | None = None
    company: str | None = None
    requirement: str | None = None
    budget: str | None = None
    timeline: str | None = None
    location: str | None = None
    status: str | None = None
    assigned_expert_id: str | None = None


class LeadOut(BaseModel):
    id: str
    business_id: str | None = ""
    source: str | None = "MANUAL"
    first_name: str | None = ""
    last_name: str | None = ""
    email: str | None = ""
    phone: str | None = ""
    company: str | None = ""
    job_title: str | None = ""
    requirement: str | None = ""
    budget: str | None = ""
    timeline: str | None = ""
    location: str | None = ""
    status: str | None = "NEW"
    score: int | None = 0
    qualification_status: str | None = "PENDING"
    ai_summary: str | None = ""
    assigned_expert_id: str | None = None
    agent_id: str | None = None
    group_id: str | None = None
    original_filename: str | None = None
    call_status: str | None = None
    call_date_time: datetime | None = None
    call_duration: int | None = 0
    twilio_call_sid: str | None = None
    call_result: str | None = None
    assigned_agent_name: str | None = None
    agent_name: str | None = None
    created_by_role: str | None = "USER"
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class WebhookLeadCreate(BaseModel):
    """Payload accepted by /api/webhooks/leads/{source} (module 10)."""

    first_name: str = ""
    last_name: str = ""
    email: str = ""
    phone: str = ""
    company: str = ""
    requirement: str = ""
    agent_id: str | None = None
    business_id: str
