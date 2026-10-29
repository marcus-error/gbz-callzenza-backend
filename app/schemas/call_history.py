"""
Call History v2 — Pydantic response schemas.
New file, no changes to existing schemas.
"""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel


class RecordingSegmentSchema(BaseModel):
    segment_index: int
    duration_ms: Optional[int] = None
    format: str = "wav"
    status: str  # ready | processing | failed | not_recorded


class RecordingInfoSchema(BaseModel):
    status: str  # ready | processing | failed | not_recorded | synthetic
    is_synthetic: bool = False  # True = AI TTS replay, not real audio
    segments: List[RecordingSegmentSchema] = []


class CallHistoryItemSchema(BaseModel):
    call_id: str
    direction: str
    status: str
    customer_name: Optional[str] = None
    customer_phone: Optional[str] = None  # masked per role
    duration_seconds: int = 0
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    ai_agent_id: Optional[str] = None
    ai_agent_name: Optional[str] = None
    human_agent_id: Optional[str] = None
    human_agent_name: Optional[str] = None
    agent_type: str = "AI_VOICE"
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    has_transcript: bool = False
    call_result: Optional[str] = None
    recording: RecordingInfoSchema


class CallHistoryListSchema(BaseModel):
    items: List[CallHistoryItemSchema]
    total: int
    next_cursor: Optional[str] = None  # keyset pagination cursor


class CallHistoryDetailSchema(CallHistoryItemSchema):
    transcript: Optional[str] = None
    ai_summary: Optional[str] = None
    turn_count: int = 0
    call_sid: Optional[str] = None
    from_number: Optional[str] = None
    to_number: Optional[str] = None


class FilterOptionsSchema(BaseModel):
    campaigns: List[dict] = []   # [{id, name}]
    ai_agents: List[dict] = []   # [{id, name}]
    human_agents: List[dict] = [] # [{id, name}]
    statuses: List[str] = []
    directions: List[str] = ["OUTBOUND", "INBOUND"]


class StreamUrlSchema(BaseModel):
    token: str
    stream_url: str
    download_url: str
    expires_in_seconds: int
    is_synthetic: bool = False
