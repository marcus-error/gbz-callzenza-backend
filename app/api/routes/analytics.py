from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional

from app.core.deps import get_current_business_id
from app.db.database import get_db
from app.db.models import AgentAssignmentQueue, Call, Conversation, Deal, Expert, Lead, LeadAssignment, AIAgent, CampaignLead
from app.schemas.analytics import DashboardSummary

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/dashboard", response_model=DashboardSummary)
async def dashboard(
    campaign_id: Optional[str] = Query(None),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> DashboardSummary:
    """Module 29/33 — every number here is computed from real DB rows."""
    lead_filter_stmt = select(CampaignLead.lead_id).where(CampaignLead.campaign_id == campaign_id) if campaign_id else None

    if lead_filter_stmt is not None:
        total_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id, Lead.id.in_(lead_filter_stmt))) or 0
        new_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id, Lead.status == "NEW", Lead.id.in_(lead_filter_stmt))) or 0
        qualified_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id, Lead.qualification_status == "QUALIFIED", Lead.id.in_(lead_filter_stmt))) or 0
    else:
        total_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id)) or 0
        new_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id, Lead.status == "NEW")) or 0
        qualified_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.business_id == business_id, Lead.qualification_status == "QUALIFIED")) or 0

    active_conversations = (
        await db.scalar(
            select(func.count(Conversation.id)).where(
                Conversation.business_id == business_id, Conversation.status == "ACTIVE"
            )
        )
        or 0
    )
    handoffs = (
        await db.scalar(
            select(func.count(LeadAssignment.id)).where(LeadAssignment.business_id == business_id)
        )
        or 0
    )
    open_deals = (
        await db.scalar(
            select(func.count(Deal.id)).where(
                Deal.business_id == business_id, Deal.stage.notin_(["WON", "LOST"])
            )
        )
        or 0
    )
    won_deals = (
        await db.scalar(
            select(func.count(Deal.id)).where(Deal.business_id == business_id, Deal.stage == "WON")
        )
        or 0
    )
    revenue = (
        await db.scalar(
            select(func.coalesce(func.sum(Deal.value), 0)).where(
                Deal.business_id == business_id, Deal.stage == "WON"
            )
        )
        or 0
    )

    # Extended Real-Time Lead Assignment & Call Metrics
    HUMAN_ACTIVE_STATUSES = [
        "HUMAN_HANDLING", "CONFERENCE_3WAY_ACTIVE", "ON_CALL", "TALKING",
        "CLIENT_ADDING", "TRANSFERRING", "TRANSFER_REQUESTED"
    ]
    AI_IVR_STATUSES = ["AI_HANDLING", "IVR", "IN_IVR", "IVR_FLOW"]
    EXCLUDED_FROM_IVRC = ["RINGING", "DIALING", "QUEUED", "WAITING", "COMPLETED", "FAILED", "NO_ANSWER", "BUSY", "CANCELLED"] + HUMAN_ACTIVE_STATUSES

    active_calls_query = select(func.count(Call.id)).where(
        Call.business_id == business_id,
        or_(
            Call.status.in_(HUMAN_ACTIVE_STATUSES),
            and_(Call.agent_type == "HUMAN", Call.status == "IN_PROGRESS")
        )
    )
    calls_ringing_query = select(func.count(Call.id)).where(
        Call.business_id == business_id, Call.status.in_(["RINGING", "DIALING"])
    )
    ivr_calls_query = select(func.count(Call.id)).where(
        Call.business_id == business_id,
        or_(
            Call.status.in_(AI_IVR_STATUSES),
            and_(Call.agent_type != "HUMAN", Call.status == "IN_PROGRESS")
        ),
        Call.status.notin_(EXCLUDED_FROM_IVRC)
    )
    completed_calls_query = select(func.count(Call.id)).where(
        Call.business_id == business_id, Call.status.in_(["COMPLETED", "CLOSED"])
    )
    queued_calls_query = select(func.count(Call.id)).where(
        Call.business_id == business_id, Call.status.in_(["QUEUED", "WAITING", "WAITING_FOR_AGENT"])
    )
    queue_item_query = select(func.count(AgentAssignmentQueue.id)).where(
        AgentAssignmentQueue.business_id == business_id, AgentAssignmentQueue.status == "WAITING"
    )

    if campaign_id:
        active_calls_query = active_calls_query.where(Call.campaign_id == campaign_id)
        calls_ringing_query = calls_ringing_query.where(Call.campaign_id == campaign_id)
        ivr_calls_query = ivr_calls_query.where(Call.campaign_id == campaign_id)
        completed_calls_query = completed_calls_query.where(Call.campaign_id == campaign_id)
        queued_calls_query = queued_calls_query.where(Call.campaign_id == campaign_id)
        queue_item_query = queue_item_query.where(AgentAssignmentQueue.campaign_id == campaign_id)

    active_calls_count = (await db.scalar(active_calls_query)) or 0
    calls_ringing_count = (await db.scalar(calls_ringing_query)) or 0
    ivr_calls_count = (await db.scalar(ivr_calls_query)) or 0
    ai_calls_in_progress = active_calls_count + calls_ringing_count + ivr_calls_count
    completed_ai_calls = (await db.scalar(completed_calls_query)) or 0

    if lead_filter_stmt is not None:
        assigned_leads = (
            await db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id, Lead.assigned_expert_id.isnot(None), Lead.id.in_(lead_filter_stmt)
                )
            )
            or 0
        )
        unassigned_leads = (
            await db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id, Lead.assigned_expert_id.is_(None), Lead.id.in_(lead_filter_stmt)
                )
            )
            or 0
        )
    else:
        assigned_leads = (
            await db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id, Lead.assigned_expert_id.isnot(None)
                )
            )
            or 0
        )
        unassigned_leads = (
            await db.scalar(
                select(func.count(Lead.id)).where(
                    Lead.business_id == business_id, Lead.assigned_expert_id.is_(None)
                )
            )
            or 0
        )

    queue_count_db = (await db.scalar(queue_item_query)) or 0
    queued_calls_db = (await db.scalar(queued_calls_query)) or 0
    waiting_queue_count = max(queue_count_db, queued_calls_db)
    
    available_agents_count = (
        await db.scalar(
            select(func.count(Expert.id)).where(
                Expert.business_id == business_id, Expert.status.in_(["AVAILABLE", "FREE"])
            )
        )
        or 0
    )
    busy_agents_count = (
        await db.scalar(
            select(func.count(Expert.id)).where(
                Expert.business_id == business_id, Expert.status.in_(["BUSY", "IN_CALL", "ON_CALL"])
            )
        )
        or 0
    )
    offline_agents_count = (
        await db.scalar(
            select(func.count(Expert.id)).where(
                Expert.business_id == business_id, Expert.status == "OFFLINE"
            )
        )
        or 0
    )
    active_transfers_count = (
        await db.scalar(
            select(func.count(LeadAssignment.id)).where(
                LeadAssignment.business_id == business_id, LeadAssignment.status == "ACTIVE"
            )
        )
        or 0
    )
    voice_agents_count = (
        await db.scalar(
            select(func.count(Expert.id)).where(
                Expert.business_id == business_id, Expert.status != "DELETED"
            )
        )
        or 0
    )
    ai_agents_count = (
        await db.scalar(
            select(func.count(AIAgent.id)).where(
                AIAgent.business_id == business_id, AIAgent.status != "DELETED"
            )
        )
        or 0
    )
    total_created_agents_count = voice_agents_count + ai_agents_count

    try:
        source_rows = await db.execute(
            select(Lead.source, func.count(Lead.id)).where(Lead.business_id == business_id).group_by(Lead.source)
        )
        lead_sources = {str(row[0] or "Direct"): int(row[1] or 0) for row in source_rows.all()}

        stage_rows = await db.execute(
            select(Deal.stage, func.count(Deal.id)).where(Deal.business_id == business_id).group_by(Deal.stage)
        )
        pipeline_by_stage = {str(row[0] or "UNKNOWN"): int(row[1] or 0) for row in stage_rows.all()}

        qualification_rate = round((qualified_leads / total_leads) * 100, 1) if total_leads else 0.0
        conversion_rate = round((won_deals / total_leads) * 100, 1) if total_leads else 0.0

        return DashboardSummary(
            total_leads=int(total_leads or 0),
            new_leads=int(new_leads or 0),
            qualified_leads=int(qualified_leads or 0),
            qualification_rate=float(qualification_rate),
            active_conversations=int(active_conversations or 0),
            human_handoffs=int(handoffs or 0),
            open_deals=int(open_deals or 0),
            won_deals=int(won_deals or 0),
            revenue=float(revenue or 0.0),
            conversion_rate=float(conversion_rate),
            lead_sources=lead_sources,
            pipeline_by_stage=pipeline_by_stage,
            active_calls_count=int(active_calls_count or 0),
            calls_ringing_count=int(calls_ringing_count or 0),
            ai_calls_in_progress=int(ai_calls_in_progress or 0),
            completed_ai_calls=int(completed_ai_calls or 0),
            assigned_leads=int(assigned_leads or 0),
            unassigned_leads=int(unassigned_leads or 0),
            waiting_queue_count=int(waiting_queue_count or 0),
            ivr_calls_count=int(ivr_calls_count or 0),
            available_agents_count=int(available_agents_count or 0),
            busy_agents_count=int(busy_agents_count or 0),
            offline_agents_count=int(offline_agents_count or 0),
            active_transfers_count=int(active_transfers_count or 0),
            total_created_agents_count=int(total_created_agents_count or 0),
            voice_agents_count=int(voice_agents_count or 0),
            ai_agents_count=int(ai_agents_count or 0),
        )
    except Exception as e:
        from loguru import logger
        logger.error(f"[DASHBOARD ANALYTICS ERROR] {e}")
        return DashboardSummary(
            total_leads=0,
            new_leads=0,
            qualified_leads=0,
            qualification_rate=0.0,
            active_conversations=0,
            human_handoffs=0,
            open_deals=0,
            won_deals=0,
            revenue=0.0,
            conversion_rate=0.0,
            lead_sources={},
            pipeline_by_stage={},
            active_calls_count=0,
            calls_ringing_count=0,
            ai_calls_in_progress=0,
            completed_ai_calls=0,
            assigned_leads=0,
            unassigned_leads=0,
            waiting_queue_count=0,
            ivr_calls_count=0,
            available_agents_count=0,
            busy_agents_count=0,
            offline_agents_count=0,
            active_transfers_count=0,
            total_created_agents_count=0,
            voice_agents_count=0,
            ai_agents_count=0,
        )

