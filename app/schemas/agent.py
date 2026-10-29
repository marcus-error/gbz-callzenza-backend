from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class AgentCreate(BaseModel):
    name: str
    description: str = ""
    type: str = "SALES"
    voice_config_id: Optional[str] = None
    voice_name: Optional[str] = None
    voice_provider: str = "elevenlabs"
    voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    voice_stability: float = Field(default=0.5, ge=0.0, le=1.0)
    voice_similarity_boost: float = Field(default=0.75, ge=0.0, le=1.0)
    voice_model_id: str = "eleven_multilingual_v2"
    language: str = "en"
    system_prompt: str = "You are a helpful AI sales agent."
    qualification_prompt: str = ""
    working_hours: dict = {}
    transfer_enabled: bool = True
    transfer_number: str = ""


class AgentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    type: str | None = None
    status: str | None = None
    voice_config_id: str | None = None
    voice_name: str | None = None
    voice_provider: str | None = None
    voice_id: str | None = None
    voice_stability: float | None = Field(default=None, ge=0.0, le=1.0)
    voice_similarity_boost: float | None = Field(default=None, ge=0.0, le=1.0)
    voice_model_id: str | None = None
    language: str | None = None
    system_prompt: str | None = None
    qualification_prompt: str | None = None
    working_hours: dict | None = None
    transfer_enabled: bool | None = None
    transfer_number: str | None = None


class AgentOut(BaseModel):
    id: str
    business_id: str
    name: str
    description: str
    type: str
    status: str
    voice_config_id: Optional[str] = None
    voice_name: Optional[str] = None
    voice_provider: str
    voice_id: str
    voice_stability: float
    voice_similarity_boost: float
    voice_model_id: str
    language: str
    system_prompt: str
    qualification_prompt: str
    working_hours: dict
    transfer_enabled: bool
    transfer_number: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class VoicePreviewRequest(BaseModel):
    text: str = "Hi, this is a quick preview of how I sound on a call."
    voice_config_id: Optional[str] = None
    voice_provider: Optional[str] = None
    voice_id: Optional[str] = None
    voice_model_id: Optional[str] = None
    gender: Optional[str] = None
    lang: Optional[str] = None
