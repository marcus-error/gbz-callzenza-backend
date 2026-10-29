from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Filter Query Schema
# ---------------------------------------------------------------------------

class DashboardFiltersQuery(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    date_range: Optional[str] = None  # today, yesterday, last_7_days, last_30_days, custom
    status: Optional[List[str]] = None
    disposition: Optional[List[str]] = None
    lead_library_id: Optional[List[str]] = None
    sentiment: Optional[List[str]] = None
    duration_min: Optional[int] = None
    duration_max: Optional[int] = None
    retry_count: Optional[int] = None
    search: Optional[str] = None
    sort_by: Optional[str] = "created_at"
    sort_order: Optional[str] = "desc"
    page: int = 1
    page_size: int = 20
    tz: Optional[str] = "UTC"


# ---------------------------------------------------------------------------
# Header Schema
# ---------------------------------------------------------------------------

class AgentHeaderInfo(BaseModel):
    id: str
    name: str
    type: str = "AI Agent"  # "AI Agent" or "Voice Agent"
    owner: str = "—"
    source: str = "Lead Library"
    phone_number: str = "—"
    assigned_agent: Optional[str] = None
    client_name: Optional[str] = None
    status: str = "IDLE"  # IDLE, RUNNING, PAUSED, COMPLETED
    created_at: Optional[datetime] = None
    last_updated_at: Optional[datetime] = None
    system_prompt: Optional[str] = None
    voice_id: Optional[str] = None
    language: Optional[str] = "en"


# ---------------------------------------------------------------------------
# KPI & Summary Schemas
# ---------------------------------------------------------------------------

class CostMetric(BaseModel):
    available: bool = False
    amount: Optional[float] = None
    reason: Optional[str] = "Per-call cost credits are not recorded on call records in database."


class AgentKpis(BaseModel):
    total_calls: int = 0
    connected_calls: int = 0
    answered_calls: int = 0
    missed_failed_calls: int = 0
    avg_call_duration_seconds: int = 0
    avg_call_duration_formatted: str = "00:00"
    success_rate_percentage: float = 0.0
    leads_in_queue: int = 0
    leads_completed: int = 0
    cost_metric: CostMetric = Field(default_factory=CostMetric)
    total_cost_credits: Optional[float] = None  # Backwards compatibility helper


class ChartDataPoint(BaseModel):
    label: str
    value: int = 0
    secondary_value: Optional[int] = 0


class HourlyActivityPoint(BaseModel):
    hour: int
    day_of_week: int  # 0=Mon, 6=Sun
    count: int = 0


class AgentDashboardSummary(BaseModel):
    agent: AgentHeaderInfo
    kpis: AgentKpis
    outcome_distribution: Dict[str, int] = Field(default_factory=dict)
    hourly_activity: List[HourlyActivityPoint] = Field(default_factory=list)
    call_volume_trend: List[ChartDataPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Calls Schemas
# ---------------------------------------------------------------------------

class CallItem(BaseModel):
    call_id: str
    lead_id: Optional[str] = None
    lead_name: str = "Unknown Lead"
    phone_number: str = "—"
    start_time: Optional[datetime] = None
    duration_seconds: int = 0
    duration_formatted: str = "00:00"
    status: str = "COMPLETED"
    outcome: str = "—"
    disposition: str = "—"
    sentiment: str = "NEUTRAL"  # POSITIVE, NEUTRAL, NEGATIVE
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    ai_summary: Optional[str] = None
    retry_count: int = 0


class CallsPageResponse(BaseModel):
    items: List[CallItem] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    total_pages: int = 1


class CallDetailResponse(BaseModel):
    call_id: str
    campaign_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: str = "AI_VOICE"
    lead_id: Optional[str] = None
    lead_name: str = "Unknown Lead"
    phone_number: str = "—"
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    duration_seconds: int = 0
    duration_formatted: str = "00:00"
    status: str = "COMPLETED"
    disposition: str = "—"
    sentiment: str = "NEUTRAL"
    recording_url: Optional[str] = None
    transcript: Optional[str] = None
    ai_summary: Optional[str] = None
    turn_count: int = 0
    hangup_reason: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Queue Schemas
# ---------------------------------------------------------------------------

class QueueItem(BaseModel):
    id: str
    lead_id: str
    lead_name: str = "Lead"
    phone_number: str = "—"
    position: int = 1
    scheduled_time: Optional[datetime] = None
    attempts: int = 0
    next_retry_time: Optional[datetime] = None
    status: str = "PENDING"


class QueuePageResponse(BaseModel):
    items: List[QueueItem] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    total_pages: int = 1


# ---------------------------------------------------------------------------
# History Schemas
# ---------------------------------------------------------------------------

class HistoryItem(BaseModel):
    id: str
    timestamp: datetime
    event_type: str
    title: str
    description: str = ""
    actor: str = "System"
    details: Optional[Dict[str, Any]] = None
    badge_color: Optional[str] = "indigo"


class HistoryPageResponse(BaseModel):
    items: List[HistoryItem] = Field(default_factory=list)
    page: int = 1
    page_size: int = 20
    total: int = 0
    total_pages: int = 1


# ---------------------------------------------------------------------------
# Reports Schemas
# ---------------------------------------------------------------------------

class ReportBucket(BaseModel):
    date_label: str
    total_calls: int = 0
    connected_calls: int = 0
    failed_calls: int = 0
    avg_duration_seconds: float = 0.0
    success_rate_percentage: float = 0.0


class ReportsResponse(BaseModel):
    granularity: str = "day"  # day, week, month
    items: List[ReportBucket] = Field(default_factory=list)
    total_calls: int = 0
    overall_success_rate: float = 0.0


# ---------------------------------------------------------------------------
# Transcripts Search Schema
# ---------------------------------------------------------------------------

class TranscriptSearchItem(BaseModel):
    call_id: str
    lead_name: str
    phone_number: str
    start_time: Optional[datetime] = None
    duration_formatted: str = "00:00"
    matched_snippet: str
    full_transcript: Optional[str] = None
    recording_url: Optional[str] = None
    sentiment: str = "NEUTRAL"
    outcome: str = "—"


class TranscriptsSearchResponse(BaseModel):
    items: List[TranscriptSearchItem] = Field(default_factory=list)
    total: int = 0
    query: str = ""


# ---------------------------------------------------------------------------
# Filter Options Schema
# ---------------------------------------------------------------------------

class FilterOptionItem(BaseModel):
    label: str
    value: str
    count: Optional[int] = None


class FilterOptionsResponse(BaseModel):
    statuses: List[FilterOptionItem] = Field(default_factory=list)
    dispositions: List[FilterOptionItem] = Field(default_factory=list)
    lead_libraries: List[FilterOptionItem] = Field(default_factory=list)
    sentiments: List[FilterOptionItem] = Field(default_factory=list)
