from datetime import datetime
from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str = Field(..., examples=["Skyline Real Estate Agent"])
    description: str | None = Field("", examples=["Assists users with Skyline Realty property specifications"])
    system_prompt: str | None = Field("", examples=["You are a helpful realty agent. Provide details on villas and pricing."])


class AgentOut(BaseModel):
    id: str = Field(..., examples=["1913047d-34b3-4251-9c9e-46016c03b900"])
    name: str = Field(..., examples=["Skyline Real Estate Agent"])
    description: str = Field(..., examples=["Assists users with Skyline Realty property specifications"])
    system_prompt: str = Field(..., examples=["You are a helpful realty agent. Provide details on villas and pricing."])
    created_at: datetime

    model_config = {"from_attributes": True}


class AskRequest(BaseModel):
    question: str = Field(..., examples=["What is the price of the 2-bedroom villa in Chennai?"])
    session_id: str | None = Field(None, examples=["3a3256fa-c64e-436a-ba17-bc5dcac1eff4"])
    lead_id: str | None = Field(None, examples=["3a3256fa-c64e-436a-ba17-bc5dcac1eff4"])


class AskResponse(BaseModel):
    answer: str = Field(..., examples=["The price of the 2-bedroom villa in Chennai is $150,000."])
    session_id: str = Field(..., examples=["3a3256fa-c64e-436a-ba17-bc5dcac1eff4"])
    retrieved_context: str | None = Field(None, examples=["Previous Q: What villas are in Chennai?\nPrevious A: Skyline Villas."])


class ScriptUploadPayload(BaseModel):
    script_text: str = Field(..., examples=["Q: What is CallZenza?\nA: It's an AI-calling platform."])
    filename: str | None = Field(None, examples=["company_faq.pdf"])

