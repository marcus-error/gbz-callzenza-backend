from datetime import datetime
from pydantic import BaseModel


class DealCreate(BaseModel):
    lead_id: str
    expert_id: str | None = None
    title: str = ""
    value: float = 0
    currency: str = "USD"
    probability: int = 10
    expected_close_date: datetime | None = None


class DealUpdate(BaseModel):
    stage: str | None = None
    value: float | None = None
    probability: int | None = None
    expected_close_date: datetime | None = None


class DealOut(BaseModel):
    id: str
    business_id: str
    lead_id: str
    expert_id: str | None
    title: str
    value: float
    currency: str
    stage: str
    probability: int
    expected_close_date: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
