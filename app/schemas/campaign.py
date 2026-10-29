import re
from datetime import datetime
from pydantic import BaseModel, ConfigDict, field_validator
from typing import Any, Optional, List

E164_REGEX = re.compile(r"^\+?[1-9]\d{1,14}$")

class MurfVoiceConfig(BaseModel):
    voice_id: Optional[str] = "Pooja"
    model: Optional[str] = "falcon"
    style: Optional[str] = "Conversational"
    rate: Optional[int] = 0
    pitch: Optional[int] = 0
    format: Optional[str] = "WAV"
    sample_rate: Optional[int] = 8000
    channel_type: Optional[str] = "MONO"
    volume_offset: Optional[float] = 0.0

class ElevenLabsVoiceConfig(BaseModel):
    voice_id: Optional[str] = "EXAVITQu4vr4xnSDxMaL"
    model_id: Optional[str] = "eleven_turbo_v2_5"
    output_format: Optional[str] = "mp3_44100_128"
    stability: Optional[float] = 0.5
    similarity_boost: Optional[float] = 0.75
    style: Optional[float] = 0.0
    speed: Optional[float] = 1.0
    use_speaker_boost: Optional[bool] = True

class FishAudioVoiceConfig(BaseModel):
    reference_id: Optional[str] = ""
    model: Optional[str] = "speech-1.5"
    format: Optional[str] = "wav"
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 0.7
    latency: Optional[str] = "balanced"
    speed: Optional[float] = 1.0
    volume: Optional[float] = 0.0

class SnapServeVoiceConfig(BaseModel):
    voice_id: Optional[str] = "ca3007f96ae7499ab87d27ea3599956a"
    format: Optional[str] = "wav"
    sample_rate: Optional[int] = 8000

class CampaignVoiceConfig(BaseModel):
    provider: Optional[str] = "murf"  # murf, elevenlabs, fish_audio, snapserve
    credential_source: Optional[str] = "env_fallback"  # saved_credential, custom_key, env_fallback
    credential_id: Optional[str] = None
    api_key: Optional[str] = None
    has_credential: Optional[bool] = False
    voice_id: Optional[str] = None
    model_id: Optional[str] = None
    language: Optional[str] = "en"
    murf: Optional[MurfVoiceConfig] = None
    elevenlabs: Optional[ElevenLabsVoiceConfig] = None
    fish_audio: Optional[FishAudioVoiceConfig] = None
    snapserve: Optional[SnapServeVoiceConfig] = None
    is_active: Optional[bool] = True

class CampaignBase(BaseModel):
    name: str
    agent_type: Optional[str] = "AI_VOICE"
    agent_id: Optional[str] = None
    voice_agent_id: Optional[str] = None
    lead_group_id: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = ""
    client_phone: Optional[str] = ""
    client_company: Optional[str] = ""
    voice_agent_name: Optional[str] = ""
    lead_group_name: Optional[str] = ""
    twilio_number: Optional[str] = ""
    carrier_id: Optional[str] = None
    trunk_id: Optional[str] = None
    caller_did_id: Optional[str] = None
    sip_account_id: Optional[str] = None
    schedule: Optional[dict[str, Any]] = None
    voice_config: Optional[dict[str, Any]] = None
    auto_dial_enabled: Optional[bool] = False
    auto_dial_ratio: Optional[int] = 1
    dialable_leads: Optional[int] = 0
    total_leads: Optional[int] = 0

    @field_validator("client_phone", mode="before")
    @classmethod
    def validate_client_phone(cls, v: Any) -> str:
        if v is None:
            return ""
        s = str(v).strip()
        if not s:
            return ""
        cleaned = re.sub(r"[\s\(\)\-\.]", "", s)
        if not E164_REGEX.match(cleaned):
            raise ValueError("Invalid client_phone format; must be valid E.164 phone number")
        return cleaned

    @field_validator("auto_dial_ratio", mode="before")
    @classmethod
    def validate_auto_dial_ratio(cls, v: Any) -> int:
        if v is None:
            return 1
        if isinstance(v, str):
            v_str = v.strip()
            if ":" in v_str:
                parts = [p.strip() for p in v_str.split(":") if p.strip()]
                nums = [int(p) for p in parts if p.isdigit()]
                if nums:
                    return max(1, max(nums))
                return 1
            try:
                return max(1, int(v_str))
            except Exception:
                return 1
        try:
            return max(1, int(v))
        except Exception:
            return 1


class CampaignCreate(CampaignBase):
    lead_ids: Optional[List[str]] = []

class CampaignUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[str] = None
    agent_type: Optional[str] = None
    agent_id: Optional[str] = None
    voice_agent_id: Optional[str] = None
    lead_group_id: Optional[str] = None
    client_id: Optional[str] = None
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    client_company: Optional[str] = None
    twilio_number: Optional[str] = None
    carrier_id: Optional[str] = None
    trunk_id: Optional[str] = None
    caller_did_id: Optional[str] = None
    sip_account_id: Optional[str] = None
    schedule: Optional[dict[str, Any]] = None
    voice_config: Optional[dict[str, Any]] = None
    lead_ids: Optional[List[str]] = None
    auto_dial_enabled: Optional[bool] = None
    auto_dial_ratio: Optional[int | str] = None
    @field_validator("client_phone", mode="before")
    @classmethod
    def validate_client_phone(cls, v: Any) -> Optional[str]:
        if v is None:
            return None
        s = str(v).strip()
        return re.sub(r"[\s\(\)\-\.]", "", s)

    @field_validator("auto_dial_ratio", mode="before")
    @classmethod
    def validate_auto_dial_ratio(cls, v: Any) -> Optional[int]:
        if v is None:
            return None
        if isinstance(v, str):
            v_str = v.strip()
            if ":" in v_str:
                parts = [p.strip() for p in v_str.split(":") if p.strip()]
                nums = [int(p) for p in parts if p.isdigit()]
                if nums:
                    return max(1, max(nums))
                return 1
            try:
                return max(1, int(v_str))
            except Exception:
                return 1
        try:
            return max(1, int(v))
        except Exception:
            return 1

class CampaignOut(CampaignBase):
    id: str
    business_id: Optional[str] = ""
    status: Optional[str] = "IDLE"
    sip_account_id: Optional[str] = None
    sip_account_name: Optional[str] = None
    sip_account_status: Optional[str] = None
    sip_account_registration: Optional[str] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)

class CampaignLeadOut(BaseModel):
    id: str
    campaign_id: str
    lead_id: str
    status: str
    call_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
