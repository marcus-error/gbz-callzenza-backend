from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.db.models import User
from app.api.v1.agent_portal_auth import get_current_portal_user
from app.api.v1.agent_dashboard import get_dashboard_filters
from app.schemas.agent_dashboard import (
    DashboardFiltersQuery,
    AgentDashboardSummary,
    CallsPageResponse,
    CallDetailResponse,
    QueuePageResponse,
    HistoryPageResponse,
    TranscriptsSearchResponse,
    FilterOptionsResponse,
)
from app.schemas.agent_portal import (
    PortalAgentsPageResponse,
    PortalRecordingsResponse,
    PortalBlacklistResponse,
    PortalVoiceResponse,
    PortalQualifiedResponse,
    PortalCompletedResponse,
)
from app.services.agent_portal_service import AgentPortalService

router = APIRouter(prefix="/api/v1/agent-portal", tags=["agent_portal_data"])
api_router = APIRouter(prefix="/api/agent-portal", tags=["agent_portal_data"])


# ---------------------------------------------------------------------------
# 1. Accessible Agents List
# ---------------------------------------------------------------------------

@router.get("/agents", response_model=PortalAgentsPageResponse)
@api_router.get("/agents", response_model=PortalAgentsPageResponse)
async def list_portal_agents(
    search: Optional[str] = Query(None, description="Search by name, phone or type"),
    status: Optional[str] = Query(None, description="Filter by status (IDLE, RUNNING, etc)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns the list of AI agents accessible to the portal user,
    scoped strictly to their tenant and assigned agent permissions.
    """
    return await AgentPortalService.list_agents(
        user=current_user,
        db=db,
        search=search,
        status_filter=status,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 1b. Campaigns using a specific Agent
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/campaigns")
@api_router.get("/agents/{agent_id}/campaigns")
async def get_agent_campaigns(
    agent_id: str,
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all campaigns that use the specified AI agent.
    Scoped to the portal user's business_id for isolation.
    """
    from sqlalchemy import text

    result = await db.execute(
        text("""
            SELECT
                c.id,
                c.name,
                c.status,
                c.agent_type,
                c.lead_group_name,
                c.started_at,
                c.created_at,
                c.updated_at,
                c.twilio_number,
                COUNT(cl.id) AS total_calls
            FROM campaigns c
            LEFT JOIN calls cl ON cl.campaign_id = c.id
            WHERE (c.agent_id = :agent_id OR c.id = :agent_id OR c.voice_agent_id = :agent_id)
              AND (c.business_id = :business_id OR :business_id IS NULL OR c.business_id IS NULL OR c.business_id = 'default')
            GROUP BY c.id
            ORDER BY c.created_at DESC
        """),
        {"agent_id": agent_id, "business_id": current_user.business_id},
    )
    rows = result.fetchall()

    campaigns = []
    for row in rows:
        campaigns.append({
            "id": row[0],
            "name": row[1],
            "status": row[2] or "IDLE",
            "agent_type": row[3] or "AI_VOICE",
            "lead_group_name": row[4] or "",
            "started_at": row[5],
            "created_at": row[6],
            "updated_at": row[7],
            "phone_number": row[8] or "",
            "total_calls": int(row[9] or 0),
        })

    return {"agent_id": agent_id, "campaigns": campaigns, "total": len(campaigns)}


# ---------------------------------------------------------------------------
# 2. Overview / KPI Summary
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/overview", response_model=AgentDashboardSummary)
@api_router.get("/agents/{agent_id}/overview", response_model=AgentDashboardSummary)
async def get_agent_portal_overview(
    agent_id: str,
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns agent header, KPIs, outcome distribution, and volume trends."""
    return await AgentPortalService.get_overview(
        agent_id=agent_id,
        user=current_user,
        db=db,
        filters=filters,
    )


# ---------------------------------------------------------------------------
# 3. Calls Handled
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/calls", response_model=CallsPageResponse)
@api_router.get("/agents/{agent_id}/calls", response_model=CallsPageResponse)
async def get_agent_portal_calls(
    agent_id: str,
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns paginated, filtered call logs for this agent."""
    return await AgentPortalService.get_calls(
        agent_id=agent_id,
        user=current_user,
        db=db,
        filters=filters,
    )


@router.get("/agents/{agent_id}/calls/{call_id}", response_model=CallDetailResponse)
@api_router.get("/agents/{agent_id}/calls/{call_id}", response_model=CallDetailResponse)
async def get_agent_portal_call_detail(
    agent_id: str,
    call_id: str,
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns single call detail and full conversation dialogue for drawer."""
    return await AgentPortalService.get_call_detail(
        agent_id=agent_id,
        call_id=call_id,
        user=current_user,
        db=db,
    )


# ---------------------------------------------------------------------------
# 4. Call History Activity Log
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/history", response_model=HistoryPageResponse)
@api_router.get("/agents/{agent_id}/history", response_model=HistoryPageResponse)
async def get_agent_portal_history(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns chronological activity log for this agent."""
    return await AgentPortalService.get_history(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 5. Call Recordings Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/recordings", response_model=PortalRecordingsResponse)
@api_router.get("/agents/{agent_id}/recordings", response_model=PortalRecordingsResponse)
async def get_agent_portal_recordings(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns call records that have active audio recordings available for playback."""
    return await AgentPortalService.get_recordings(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 6. Transcripts Search Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/transcripts", response_model=TranscriptsSearchResponse)
@api_router.get("/agents/{agent_id}/transcripts", response_model=TranscriptsSearchResponse)
async def search_agent_portal_transcripts(
    agent_id: str,
    q: Optional[str] = Query("", description="Keyword to search in conversation transcripts"),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Searches conversation transcripts with matched snippet extraction."""
    return await AgentPortalService.search_transcripts(
        agent_id=agent_id,
        user=current_user,
        db=db,
        q=q or "",
    )


# ---------------------------------------------------------------------------
# 7. Call Queue Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/queue", response_model=QueuePageResponse)
@api_router.get("/agents/{agent_id}/queue", response_model=QueuePageResponse)
async def get_agent_portal_queue(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns queued/pending leads awaiting dialing for this agent."""
    return await AgentPortalService.get_queue(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
        status_filter=status,
        search=search,
    )


# ---------------------------------------------------------------------------
# 8. Call Blacklist / DNC Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/blacklist", response_model=PortalBlacklistResponse)
@api_router.get("/agents/{agent_id}/blacklist", response_model=PortalBlacklistResponse)
async def get_agent_portal_blacklist(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    search: Optional[str] = Query(None),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns blacklisted and Do-Not-Call (DNC) numbers for this agent."""
    return await AgentPortalService.get_blacklist(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
        search=search,
    )


# ---------------------------------------------------------------------------
# 9. Voice Agent Calls & Human Transfers Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/voice", response_model=PortalVoiceResponse)
@api_router.get("/agents/{agent_id}/voice", response_model=PortalVoiceResponse)
async def get_agent_portal_voice(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns calls handled by human voice agents or warm handoff transfers."""
    return await AgentPortalService.get_voice(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 10. Qualified Leads Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/qualified", response_model=PortalQualifiedResponse)
@api_router.get("/agents/{agent_id}/qualified", response_model=PortalQualifiedResponse)
async def get_agent_portal_qualified(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns leads that have been qualified by this agent."""
    return await AgentPortalService.get_qualified(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 11. Completed Calls Tab
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/completed", response_model=PortalCompletedResponse)
@api_router.get("/agents/{agent_id}/completed", response_model=PortalCompletedResponse)
async def get_agent_portal_completed(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns all calls that completed their session."""
    return await AgentPortalService.get_completed(
        agent_id=agent_id,
        user=current_user,
        db=db,
        page=page,
        page_size=page_size,
    )


# ---------------------------------------------------------------------------
# 12. Dynamic Filter Options & Export
# ---------------------------------------------------------------------------

@router.get("/agents/{agent_id}/filter-options", response_model=FilterOptionsResponse)
@api_router.get("/agents/{agent_id}/filter-options", response_model=FilterOptionsResponse)
async def get_agent_portal_filter_options(
    agent_id: str,
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns dynamic filter options populated from real data."""
    return await AgentPortalService.get_filter_options(
        agent_id=agent_id,
        user=current_user,
        db=db,
    )


@router.get("/agents/{agent_id}/export")
@api_router.get("/agents/{agent_id}/export")
async def export_agent_portal_report(
    agent_id: str,
    type: str = Query("csv", pattern="^(csv|pdf)$"),
    tab: str = Query("calls", pattern="^(calls|queue|reports)$"),
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Exports agent report data as CSV or PDF attachment respecting active filters."""
    content = await AgentPortalService.export_stream(
        agent_id=agent_id,
        user=current_user,
        db=db,
        export_type=type,
        tab=tab,
        filters=filters,
    )

    filename = f"portal_agent_{agent_id}_{tab}.{type}"
    media_type = "text/csv" if type == "csv" else "application/pdf"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
