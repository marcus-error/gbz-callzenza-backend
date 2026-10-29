from datetime import datetime
from pydantic import BaseModel, ConfigDict
from typing import Any, Optional

class IntegrationBase(BaseModel):
    provider: str
    config: dict[str, Any]
    is_active: Optional[bool] = True

class IntegrationCreate(IntegrationBase):
    pass

class IntegrationUpdate(BaseModel):
    config: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None

class IntegrationOut(IntegrationBase):
    id: str
    business_id: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
