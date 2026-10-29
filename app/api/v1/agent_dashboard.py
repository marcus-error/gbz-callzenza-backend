from __future__ import annotations

from typing import List, Optional
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_optional_db_user
from app.db.database import get_db
from app.db.models import User
from app.schemas.agent_dashboard import (
    DashboardFiltersQuery,
    AgentHeaderInfo,
    AgentDashboardSummary,
    CallsPageResponse,
    CallDetailResponse,
    QueuePageResponse,
    HistoryPageResponse,
    ReportsResponse,
    TranscriptsSearchResponse,
    FilterOptionsResponse,
)
from app.services.agent_dashboard_service import AgentDashboardService

router = APIRouter(prefix="/api/v1/agent-dashboard", tags=["agent_dashboard_v1"])
api_router = APIRouter(prefix="/api/agent-dashboard", tags=["agent_dashboard_v1"])


def get_dashboard_filters(
    date_from: Optional[str] = Query(None, description="ISO datetime start"),
    date_to: Optional[str] = Query(None, description="ISO datetime end"),
    date_range: Optional[str] = Query(None, description="today, yesterday, last_7_days, last_30_days, custom"),
    status: Optional[List[str]] = Query(None, description="Status filters"),
    disposition: Optional[List[str]] = Query(None, description="Outcome / disposition filters"),
    lead_library_id: Optional[List[str]] = Query(None, description="Lead group filters"),
    sentiment: Optional[List[str]] = Query(None, description="Sentiment filters"),
    duration_min: Optional[int] = Query(None, ge=0),
    duration_max: Optional[int] = Query(None, ge=0),
    retry_count: Optional[int] = Query(None, ge=0),
    search: Optional[str] = Query(None, description="Search keyword"),
    sort_by: Optional[str] = Query("created_at"),
    sort_order: Optional[str] = Query("desc"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    tz: Optional[str] = Query("UTC"),
) -> DashboardFiltersQuery:
    return DashboardFiltersQuery(
        date_from=date_from,
        date_to=date_to,
        date_range=date_range,
        status=status,
        disposition=disposition,
        lead_library_id=lead_library_id,
        sentiment=sentiment,
        duration_min=duration_min,
        duration_max=duration_max,
        retry_count=retry_count,
        search=search,
        sort_by=sort_by,
        sort_order=sort_order,
        page=page,
        page_size=page_size,
        tz=tz,
    )


# ---------------------------------------------------------------------------
# 1. Header Endpoint
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/header", response_model=AgentHeaderInfo)
@api_router.get("/{agent_id}/header", response_model=AgentHeaderInfo)
async def get_agent_dashboard_header(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves metadata header for the agent/campaign."""
    return await AgentDashboardService.get_header(
        agent_id=agent_id,
        db=db,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 2. KPI Summary Endpoint
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/summary", response_model=AgentDashboardSummary)
@api_router.get("/{agent_id}/summary", response_model=AgentDashboardSummary)
async def get_agent_dashboard_summary(
    agent_id: str,
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves unified agent header, KPI metrics, and summary distributions."""
    return await AgentDashboardService.get_summary(
        agent_id=agent_id,
        db=db,
        filters=filters,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 3. Calls List Endpoint
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/calls", response_model=CallsPageResponse)
@api_router.get("/{agent_id}/calls", response_model=CallsPageResponse)
async def get_agent_dashboard_calls(
    agent_id: str,
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves paginated, filtered call logs for this agent."""
    return await AgentDashboardService.get_calls(
        agent_id=agent_id,
        db=db,
        filters=filters,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 4. Single Call Detail (For Drawer)
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/calls/{call_id}", response_model=CallDetailResponse)
@api_router.get("/{agent_id}/calls/{call_id}", response_model=CallDetailResponse)
async def get_agent_dashboard_call_detail(
    agent_id: str,
    call_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves single call record with full transcript dialogue for drawer."""
    return await AgentDashboardService.get_call_detail(
        agent_id=agent_id,
        call_id=call_id,
        db=db,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 5. Queue Endpoint
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/queue", response_model=QueuePageResponse)
@api_router.get("/{agent_id}/queue", response_model=QueuePageResponse)
async def get_agent_dashboard_queue(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves pending/queued leads for this agent's campaign."""
    return await AgentDashboardService.get_queue(
        agent_id=agent_id,
        db=db,
        page=page,
        page_size=page_size,
        status_filter=status,
        search=search,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 6. History Activity Log
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/history", response_model=HistoryPageResponse)
@api_router.get("/{agent_id}/history", response_model=HistoryPageResponse)
async def get_agent_dashboard_history(
    agent_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves chronological activity audit log for this agent."""
    return await AgentDashboardService.get_history(
        agent_id=agent_id,
        db=db,
        page=page,
        page_size=page_size,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 7. Aggregate Reports (Day, Week, Month)
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/reports", response_model=ReportsResponse)
@api_router.get("/{agent_id}/reports", response_model=ReportsResponse)
async def get_agent_dashboard_reports(
    agent_id: str,
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves aggregate reporting statistics grouped by day/week/month."""
    return await AgentDashboardService.get_reports(
        agent_id=agent_id,
        db=db,
        granularity=granularity,
        filters=filters,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 8. Transcripts Search
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/transcripts", response_model=TranscriptsSearchResponse)
@api_router.get("/{agent_id}/transcripts", response_model=TranscriptsSearchResponse)
async def search_agent_dashboard_transcripts(
    agent_id: str,
    q: Optional[str] = Query("", description="Keyword to search in transcripts"),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Searches conversation transcripts with matched snippet extraction."""
    return await AgentDashboardService.search_transcripts(
        agent_id=agent_id,
        query_str=q or "",
        db=db,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 9. Dynamic Filter Options
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/filter-options", response_model=FilterOptionsResponse)
@api_router.get("/{agent_id}/filter-options", response_model=FilterOptionsResponse)
async def get_agent_dashboard_filter_options(
    agent_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Retrieves available dynamic values for dropdown filters."""
    return await AgentDashboardService.get_filter_options(
        agent_id=agent_id,
        db=db,
        current_user=current_user,
    )


# ---------------------------------------------------------------------------
# 10. Export Data (CSV / PDF)
# ---------------------------------------------------------------------------
@router.get("/{agent_id}/export")
@api_router.get("/{agent_id}/export")
async def export_agent_dashboard(
    agent_id: str,
    type: str = Query("csv", pattern="^(csv|pdf)$"),
    tab: str = Query("calls", pattern="^(calls|queue|reports)$"),
    filters: DashboardFiltersQuery = Depends(get_dashboard_filters),
    db: AsyncSession = Depends(get_db),
    current_user: Optional[User] = Depends(get_optional_db_user),
):
    """Exports agent report data as CSV or PDF attachment respecting filters."""
    content = await AgentDashboardService.export_stream(
        agent_id=agent_id,
        db=db,
        export_type=type,
        tab=tab,
        filters=filters,
        current_user=current_user,
    )

    filename = f"agent_{agent_id}_{tab}.{type}"
    media_type = "text/csv" if type == "csv" else "application/pdf"

    return Response(
        content=content,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )
