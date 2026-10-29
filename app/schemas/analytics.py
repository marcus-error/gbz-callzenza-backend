from pydantic import BaseModel


class DashboardSummary(BaseModel):
    total_leads: int
    new_leads: int
    qualified_leads: int
    qualification_rate: float
    active_conversations: int
    human_handoffs: int
    open_deals: int
    won_deals: int
    revenue: float
    conversion_rate: float
    lead_sources: dict[str, int]
    pipeline_by_stage: dict[str, int]
    
    # Extended Real-Time System Metrics
    active_calls_count: int = 0
    calls_ringing_count: int = 0
    ai_calls_in_progress: int = 0
    completed_ai_calls: int = 0
    assigned_leads: int = 0
    unassigned_leads: int = 0
    waiting_queue_count: int = 0
    ivr_calls_count: int = 0
    available_agents_count: int = 0
    busy_agents_count: int = 0
    offline_agents_count: int = 0
    active_transfers_count: int = 0
    total_created_agents_count: int = 0
    voice_agents_count: int = 0
    ai_agents_count: int = 0


