from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class PlanOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str = ""
    price_cents: int
    billing_interval: str
    max_agents: int
    max_concurrent_calls: int
    max_leads: int
    max_minutes_monthly: int
    features: Dict[str, Any] = Field(default_factory=dict)
    is_active: bool

    model_config = {"from_attributes": True}


class PlanCreate(BaseModel):
    name: str
    slug: str
    description: str = ""
    price_cents: int = 0
    billing_interval: str = "monthly"
    max_agents: int = 5
    max_concurrent_calls: int = 2
    max_leads: int = 1000
    max_minutes_monthly: int = 300
    features: Optional[Dict[str, Any]] = None


class SubscriptionCreate(BaseModel):
    plan_id: str
    auto_renew: bool = True
    payment_method_id: Optional[str] = None


class SubscriptionOut(BaseModel):
    id: str
    tenant_id: str
    plan_id: str
    status: str
    auto_renew: bool
    start_date: datetime
    end_date: Optional[datetime] = None
    plan: Optional[PlanOut] = None

    model_config = {"from_attributes": True}
