from datetime import datetime
from pydantic import BaseModel


class CallOut(BaseModel):
    id: str
    business_id: str | None = ""
    lead_id: str | None = None
    campaign_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    call_sid: str | None = ""
    conference_sid: str | None = None
    direction: str | None = "OUTBOUND"
    agent_type: str | None = "AI_VOICE"
    client_name: str | None = ""
    client_phone: str | None = ""
    from_number: str | None = ""
    to_number: str | None = ""
    status: str | None = "QUEUED"
    duration_seconds: int | None = 0
    recording_url: str | None = ""
    transcript: str | None = ""
    turn_count: int | None = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None
    initiated_at: datetime | None = None
    connected_at: datetime | None = None
    ended_at: datetime | None = None
    formatted_duration: str | None = None

    lead_name: str | None = None
    agent_name: str | None = None

    model_config = {"from_attributes": True, "extra": "ignore"}


class CallTransferOut(BaseModel):
    id: str
    business_id: str
    call_id: str
    lead_id: str | None = None
    from_agent_id: str
    to_agent_id: str
    transfer_type: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class CallParticipantOut(BaseModel):
    id: str
    call_id: str
    participant_type: str
    participant_id: str | None = None
    joined_at: datetime
    left_at: datetime | None = None

    model_config = {"from_attributes": True}


class TriggerCallRequest(BaseModel):
    lead_id: str
    agent_id: str | None = None

