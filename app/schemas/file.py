from datetime import datetime
from typing import Optional
from pydantic import BaseModel


class TenantFileOut(BaseModel):
    id: str
    tenant_id: str
    filename: str
    original_filename: str
    file_size_bytes: int
    mime_type: str
    storage_type: str
    uploader_id: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}
