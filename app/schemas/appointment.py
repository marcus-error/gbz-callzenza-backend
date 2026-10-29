from datetime import datetime
from pydantic import BaseModel


class AppointmentCreate(BaseModel):
    lead_id: str
    agent_id: str | None = None
    expert_id: str | None = None
    scheduled_at: datetime
    notes: str = ""


class AppointmentOut(BaseModel):
    id: str
    business_id: str
    lead_id: str
    agent_id: str | None
    expert_id: str | None
    scheduled_at: datetime
    status: str
    notes: str
    created_at: datetime

    model_config = {"from_attributes": True}
