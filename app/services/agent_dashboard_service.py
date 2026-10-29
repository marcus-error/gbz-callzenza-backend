from __future__ import annotations

import csv
import io
import math
from typing import Any, Dict, Optional
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.repositories.agent_dashboard_repository import AgentDashboardRepository
from app.schemas.agent_dashboard import (
    DashboardFiltersQuery,
    AgentHeaderInfo,
    AgentDashboardSummary,
    AgentKpis,
    CallsPageResponse,
    CallItem,
    CallDetailResponse,
    QueuePageResponse,
    QueueItem,
    HistoryPageResponse,
    HistoryItem,
    ReportsResponse,
    ReportBucket,
    TranscriptsSearchResponse,
    TranscriptSearchItem,
    FilterOptionsResponse,
    FilterOptionItem,
)


class AgentDashboardService:
    """Service layer coordinating business logic, tenant validation, and reports."""

    @classmethod
    async def _resolve_and_verify(
        cls,
        agent_id: str,
        db: AsyncSession,
        current_user: Optional[User],
    ) -> Dict[str, Any]:
        agent_ctx = await AgentDashboardRepository.resolve_agent_entity(agent_id, db)
        if not agent_ctx:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent or Campaign with ID '{agent_id}' was not found.",
            )
        AgentDashboardRepository.validate_tenant_access(agent_ctx, current_user)
        return agent_ctx

    @classmethod
    async def get_header(
        cls,
        agent_id: str,
        db: AsyncSession,
        current_user: Optional[User] = None,
    ) -> AgentHeaderInfo:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        return AgentHeaderInfo(
            id=agent_id,
            name=agent_ctx.get("name") or "Agent",
            type=agent_ctx.get("agent_type") or "AI Agent",
            owner=agent_ctx.get("owner_name") or "—",
            source=agent_ctx.get("source_name") or "Lead Library",
            phone_number=agent_ctx.get("phone_number") or "—",
            assigned_agent=agent_ctx.get("assigned_agent_name"),
            client_name=agent_ctx.get("client_name"),
            status=agent_ctx.get("status") or "IDLE",
            created_at=agent_ctx.get("created_at"),
            last_updated_at=agent_ctx.get("last_updated_at"),
            system_prompt=agent_ctx.get("system_prompt"),
            voice_id=agent_ctx.get("voice_id"),
            language=agent_ctx.get("language") or "en",
        )

    @classmethod
    async def get_summary(
        cls,
        agent_id: str,
        db: AsyncSession,
        filters: DashboardFiltersQuery,
        current_user: Optional[User] = None,
    ) -> AgentDashboardSummary:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        data = await AgentDashboardRepository.get_kpis_and_summary_cached(agent_ctx, db, filters)

        header = await cls.get_header(agent_id, db, current_user)
        kpis = AgentKpis(**data["kpis"])

        return AgentDashboardSummary(
            agent=header,
            kpis=kpis,
            outcome_distribution=data["outcome_distribution"],
            hourly_activity=data["hourly_activity"],
            call_volume_trend=data["call_volume_trend"],
        )

    @classmethod
    async def get_calls(
        cls,
        agent_id: str,
        db: AsyncSession,
        filters: DashboardFiltersQuery,
        current_user: Optional[User] = None,
    ) -> CallsPageResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        items_raw, total = await AgentDashboardRepository.get_calls_page(agent_ctx, db, filters)

        items = [CallItem(**row) for row in items_raw]
        page_size = min(max(1, filters.page_size), 100)
        total_pages = max(1, math.ceil(total / page_size))

        return CallsPageResponse(
            items=items,
            page=filters.page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )

    @classmethod
    async def get_call_detail(
        cls,
        agent_id: str,
        call_id: str,
        db: AsyncSession,
        current_user: Optional[User] = None,
    ) -> CallDetailResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        call_data = await AgentDashboardRepository.get_call_by_id(agent_ctx, call_id, db)
        if not call_data:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call record with ID '{call_id}' not found.",
            )
        return CallDetailResponse(**call_data)

    @classmethod
    async def get_queue(
        cls,
        agent_id: str,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
        current_user: Optional[User] = None,
    ) -> QueuePageResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        items_raw, total = await AgentDashboardRepository.get_queue_page(
            agent_ctx,
            db,
            page=page,
            page_size=page_size,
            status=status_filter,
            search=search,
        )

        items = [QueueItem(**row) for row in items_raw]
        page_sz = min(max(1, page_size), 100)
        total_pages = max(1, math.ceil(total / page_sz))

        return QueuePageResponse(
            items=items,
            page=page,
            page_size=page_sz,
            total=total,
            total_pages=total_pages,
        )

    @classmethod
    async def get_history(
        cls,
        agent_id: str,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        current_user: Optional[User] = None,
    ) -> HistoryPageResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        items_raw, total = await AgentDashboardRepository.get_history_page(
            agent_ctx,
            db,
            page=page,
            page_size=page_size,
        )

        items = [HistoryItem(**row) for row in items_raw]
        page_sz = min(max(1, page_size), 100)
        total_pages = max(1, math.ceil(total / page_sz))

        return HistoryPageResponse(
            items=items,
            page=page,
            page_size=page_sz,
            total=total,
            total_pages=total_pages,
        )

    @classmethod
    async def get_reports(
        cls,
        agent_id: str,
        db: AsyncSession,
        granularity: str = "day",
        filters: Optional[DashboardFiltersQuery] = None,
        current_user: Optional[User] = None,
    ) -> ReportsResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        data = await AgentDashboardRepository.get_reports_aggregation(
            agent_ctx,
            db,
            granularity=granularity,
            filters=filters,
        )

        items = [ReportBucket(**b) for b in data["items"]]
        return ReportsResponse(
            granularity=data["granularity"],
            items=items,
            total_calls=data["total_calls"],
            overall_success_rate=data["overall_success_rate"],
        )

    @classmethod
    async def search_transcripts(
        cls,
        agent_id: str,
        query_str: str,
        db: AsyncSession,
        current_user: Optional[User] = None,
    ) -> TranscriptsSearchResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        if not query_str or not query_str.strip():
            return TranscriptsSearchResponse(items=[], total=0, query="")

        items_raw = await AgentDashboardRepository.search_transcripts_keyword(
            agent_ctx,
            query_str,
            db,
        )
        items = [TranscriptSearchItem(**r) for r in items_raw]
        return TranscriptsSearchResponse(items=items, total=len(items), query=query_str)

    @classmethod
    async def get_filter_options(
        cls,
        agent_id: str,
        db: AsyncSession,
        current_user: Optional[User] = None,
    ) -> FilterOptionsResponse:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        data = await AgentDashboardRepository.get_filter_options_data(agent_ctx, db)

        return FilterOptionsResponse(
            statuses=[FilterOptionItem(**s) for s in data["statuses"]],
            dispositions=[FilterOptionItem(**d) for d in data["dispositions"]],
            lead_libraries=[FilterOptionItem(**ll) for ll in data["lead_libraries"]],
            sentiments=[FilterOptionItem(**sm) for sm in data["sentiments"]],
        )

    @classmethod
    async def export_stream(
        cls,
        agent_id: str,
        db: AsyncSession,
        export_type: str = "csv",
        tab: str = "calls",
        filters: Optional[DashboardFiltersQuery] = None,
        current_user: Optional[User] = None,
    ) -> bytes:
        agent_ctx = await cls._resolve_and_verify(agent_id, db, current_user)
        filters = filters or DashboardFiltersQuery()

        output = io.StringIO()
        writer = csv.writer(output)

        if tab == "queue":
            q_items, _ = await AgentDashboardRepository.get_queue_page(
                agent_ctx, db, page=1, page_size=1000
            )
            writer.writerow(["Position", "Lead Name", "Phone", "Scheduled Time", "Attempts", "Next Retry", "Status"])
            for q in q_items:
                writer.writerow([
                    q["position"],
                    q["lead_name"],
                    q["phone_number"],
                    str(q["scheduled_time"] or ""),
                    q["attempts"],
                    str(q["next_retry_time"] or ""),
                    q["status"],
                ])
        elif tab == "reports":
            r_data = await AgentDashboardRepository.get_reports_aggregation(
                agent_ctx, db, granularity="day", filters=filters
            )
            writer.writerow(["Period", "Total Calls", "Connected Calls", "Failed Calls", "Avg Duration (s)", "Success Rate %"])
            for r in r_data["items"]:
                writer.writerow([
                    r["date_label"],
                    r["total_calls"],
                    r["connected_calls"],
                    r["failed_calls"],
                    r["avg_duration_seconds"],
                    r["success_rate_percentage"],
                ])
        else:
            calls_raw, _ = await AgentDashboardRepository.get_calls_page(
                agent_ctx, db, filters=filters
            )
            writer.writerow(["Call ID", "Lead Name", "Phone Number", "Start Time", "Duration (s)", "Status", "Disposition", "Sentiment", "Retries"])
            for c in calls_raw:
                writer.writerow([
                    c.get("call_id", ""),
                    c.get("lead_name", ""),
                    c.get("phone_number", ""),
                    str(c.get("start_time", "")),
                    c.get("duration_seconds", 0),
                    c.get("status", ""),
                    c.get("disposition", ""),
                    c.get("sentiment", ""),
                    c.get("retry_count", 0),
                ])

        return output.getvalue().encode("utf-8")
