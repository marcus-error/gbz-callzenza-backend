from datetime import datetime
from typing import Any
from pydantic import BaseModel, field_validator


class ExpertCreate(BaseModel):
    name: str
    email: str = ""
    skills: list[str] = []


class ExpertOut(BaseModel):
    id: str
    business_id: str | None = ""
    user_id: str | None = None
    name: str = ""
    email: str | None = ""
    skills: list[str] = []
    status: str | None = "AVAILABLE"
    agent_type: str | None = "INBOUND"
    direction: str | None = "INBOUND"
    active_lead_count: int | None = 0
    active_calls: int | None = 0
    calls_handled: int | None = 0
    total_leads: int | None = 0
    auto_dial_enabled: bool | None = False
    created_at: datetime | None = None

    @field_validator("skills", mode="before")
    @classmethod
    def validate_skills(cls, v: Any) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v] if v.strip() else []
        if isinstance(v, list):
            return [str(item) for item in v if item is not None]
        return []

    @field_validator("status", mode="before")
    @classmethod
    def validate_status(cls, v: Any) -> str:
        return str(v) if v else "AVAILABLE"

    @field_validator("business_id", "email", "name", mode="before")
    @classmethod
    def validate_str_fields(cls, v: Any) -> str:
        if v is None:
            return ""
        return str(v)

    @field_validator("active_lead_count", "active_calls", "calls_handled", "total_leads", mode="before")
    @classmethod
    def validate_ints(cls, v: Any) -> int:
        if v is None:
            return 0
        try:
            return int(v)
        except Exception:
            return 0

    @field_validator("auto_dial_enabled", mode="before")
    @classmethod
    def validate_bool(cls, v: Any) -> bool:
        if v is None:
            return False
        return bool(v)

    model_config = {"from_attributes": True}

