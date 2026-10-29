from datetime import datetime
from pydantic import BaseModel


class LeadGroupCreate(BaseModel):
    filename: str
    leads: list[dict]
    agent_id: str | None = None


class LeadGroupOut(BaseModel):
    id: str
    business_id: str | None = ""
    user_id: str | None = None
    filename: str | None = ""
    lead_count: int | None = 0
    status: str | None = "IDLE"
    created_at: datetime | None = None

    model_config = {"from_attributes": True}
