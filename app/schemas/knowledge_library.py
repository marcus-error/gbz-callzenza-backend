from datetime import datetime
from pydantic import BaseModel, Field


class KnowledgeCreate(BaseModel):
    agent_id: str = Field(..., examples=["1913047d-34b3-4251-9c9e-46016c03b900"])
    session_id: str | None = Field(None, examples=["3a3256fa-c64e-436a-ba17-bc5dcac1eff4"])
    question: str = Field(..., examples=["What is the price of the 2-bedroom villa in Chennai?"])
    answer: str = Field(..., examples=["The price of the 2-bedroom villa in Chennai is $150,000."])
    source_data: str | None = Field("", examples=["Retrieved context description or previous Q/A."])
    sources: list[dict] | None = Field([], examples=[{"source_type": "PDF", "content": "Skyline villa specifications document"}])
    metadata: dict | None = Field({}, examples=[{"category": "pricing", "location": "Chennai"}])


class KnowledgeOut(BaseModel):
    id: str = Field(..., examples=["524f8ec1-b3b9-47d0-aa09-0379950d3e29"])
    agent_id: str = Field(..., examples=["1913047d-34b3-4251-9c9e-46016c03b900"])
    session_id: str | None = Field(None, examples=["3a3256fa-c64e-436a-ba17-bc5dcac1eff4"])
    question: str = Field(..., examples=["What is the price of the 2-bedroom villa in Chennai?"])
    answer: str = Field(..., examples=["The price of the 2-bedroom villa in Chennai is $150,000."])
    source_data: str | None = Field(None, examples=["Retrieved context description or previous Q/A."])
    created_at: datetime

    model_config = {"from_attributes": True}
