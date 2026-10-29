from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy import select, func, and_, or_, desc, asc, case, String, Integer, cast, text
from sqlalchemy import extract
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import (
    Campaign,
    AIAgent,
    Expert,
    Call,
    CampaignLead,
    Lead,
    LeadGroup,
    Client,
    AuditLog,
    User,
)
from app.schemas.agent_dashboard import DashboardFiltersQuery
from app.db.database import engine as _db_engine

# Short in-memory cache for auto-refresh hammering protection (15s TTL)
_SUMMARY_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
CACHE_TTL_SECONDS = 15.0

# -----------------------------------------------------------------------
# Dialect flag — True when running against PostgreSQL (VPS), False for SQLite (local)
# -----------------------------------------------------------------------
_IS_POSTGRES: bool = _db_engine.dialect.name not in ("sqlite",)


def _parse_dates(filters: DashboardFiltersQuery) -> Tuple[Optional[datetime], Optional[datetime]]:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None

    dr = filters.date_range
    if dr == "today":
        start_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    elif dr == "yesterday":
        yesterday = now - timedelta(days=1)
        start_dt = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        end_dt = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    elif dr in ("last_7_days", "7d"):
        start_dt = now - timedelta(days=7)
        end_dt = now
    elif dr in ("last_30_days", "30d"):
        start_dt = now - timedelta(days=30)
        end_dt = now
    else:
        if filters.date_from:
            try:
                start_dt = datetime.fromisoformat(filters.date_from.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
        if filters.date_to:
            try:
                end_dt = datetime.fromisoformat(filters.date_to.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass

    return start_dt, end_dt


def escape_like(val: str) -> str:
    """Escapes SQL LIKE wildcard characters for safe queries."""
    return val.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _is_postgres(db: AsyncSession) -> bool:
    """Detects whether the current DB engine is PostgreSQL (kept for backward compat)."""
    return _IS_POSTGRES


def _date_trunc_day(col, is_pg: bool = None):
    """Returns DB-agnostic expression for DATE(col)."""
    if is_pg is None:
        is_pg = _IS_POSTGRES
    if is_pg:
        return func.to_char(col, 'YYYY-MM-DD')
    return func.strftime("%Y-%m-%d", col)


def _date_trunc_hour(col, is_pg: bool = None):
    """Returns DB-agnostic expression for HOUR(col) as integer."""
    if is_pg is None:
        is_pg = _IS_POSTGRES
    if is_pg:
        return cast(extract('hour', col), Integer)
    return cast(func.strftime("%H", col), Integer)


def _date_trunc_dow(col, is_pg: bool = None):
    """Returns DB-agnostic day-of-week (0=Sunday) as integer."""
    if is_pg is None:
        is_pg = _IS_POSTGRES
    if is_pg:
        # PostgreSQL: 0=Sunday via EXTRACT(DOW)
        return cast(extract('dow', col), Integer)
    return cast(func.strftime("%w", col), Integer)


def _date_fmt_week(col, is_pg: bool = None):
    """Returns DB-agnostic expression for YYYY-WNN."""
    if is_pg is None:
        is_pg = _IS_POSTGRES
    if is_pg:
        return func.to_char(col, 'IYYY-\"W\"IW')
    return func.strftime("%Y-W%W", col)


def _date_fmt_month(col, is_pg: bool = None):
    """Returns DB-agnostic expression for YYYY-MM."""
    if is_pg is None:
        is_pg = _IS_POSTGRES
    if is_pg:
        return func.to_char(col, 'YYYY-MM')
    return func.strftime("%Y-%m", col)


class AgentDashboardRepository:
    """High-performance read-only repository with SQL aggregations and tenant isolation."""

    @staticmethod
    async def resolve_agent_entity(agent_id: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
        # 1. Resolve as Campaign
        stmt = (
            select(Campaign)
            .where(Campaign.id == agent_id)
        )
        res = await db.execute(stmt)
        campaign = res.scalars().first()

        if campaign:
            ai_agent = None
            if campaign.agent_id:
                ai_agent = (await db.execute(select(AIAgent).where(AIAgent.id == campaign.agent_id))).scalars().first()

            expert = None
            if campaign.voice_agent_id:
                expert = (await db.execute(select(Expert).where(Expert.id == campaign.voice_agent_id))).scalars().first()

            lead_group = None
            if campaign.lead_group_id:
                lead_group = (await db.execute(select(LeadGroup).where(LeadGroup.id == campaign.lead_group_id))).scalars().first()

            owner_user = None
            if campaign.user_id:
                owner_user = (await db.execute(select(User).where(User.id == campaign.user_id))).scalars().first()

            agent_type_str = "Voice Agent" if (
                (campaign.agent_type or "").upper() in ("HUMAN", "VOICE", "VOICE_AGENT")
                or (not campaign.agent_id and bool(campaign.voice_agent_id))
            ) else "AI Agent"

            return {
                "entity_type": "CAMPAIGN",
                "campaign_id": campaign.id,
                "business_id": campaign.business_id,
                "campaign": campaign,
                "ai_agent": ai_agent,
                "expert": expert,
                "lead_group": lead_group,
                "assigned_agent_name": (expert.name if expert else (ai_agent.name if ai_agent else None)),
                "client_name": campaign.client_name or "—",
                "owner_name": (owner_user.full_name if owner_user else campaign.client_name) or "—",
                "source_name": lead_group.filename if lead_group else "Lead Library",
                "phone_number": campaign.twilio_number or "—",
                "agent_type": agent_type_str,
                "status": campaign.status or "IDLE",
                "name": campaign.name,
                "created_at": campaign.created_at,
                "last_updated_at": campaign.updated_at,
                "system_prompt": ai_agent.system_prompt if ai_agent else None,
                "voice_id": ai_agent.voice_id if ai_agent else None,
                "language": ai_agent.language if ai_agent else "en",
            }

        # 2. Resolve as AIAgent directly
        ai_agent = (await db.execute(select(AIAgent).where(AIAgent.id == agent_id))).scalars().first()
        if ai_agent:
            camp_stmt = select(Campaign).where(Campaign.agent_id == ai_agent.id).order_by(desc(Campaign.created_at)).limit(1)
            campaign = (await db.execute(camp_stmt)).scalars().first()

            return {
                "entity_type": "AI_AGENT",
                "campaign_id": campaign.id if campaign else None,
                "business_id": ai_agent.business_id,
                "campaign": campaign,
                "ai_agent": ai_agent,
                "expert": None,
                "lead_group": None,
                "owner_name": campaign.client_name if campaign else "—",
                "source_name": "AI Agent Library",
                "phone_number": campaign.twilio_number if campaign else "—",
                "agent_type": "AI Agent",
                "status": ai_agent.status or "ACTIVE",
                "name": ai_agent.name,
                "created_at": ai_agent.created_at,
                "last_updated_at": ai_agent.updated_at,
                "system_prompt": ai_agent.system_prompt,
                "voice_id": ai_agent.voice_id,
                "language": ai_agent.language or "en",
            }

        # 3. Resolve as Expert directly
        expert = (await db.execute(select(Expert).where(Expert.id == agent_id))).scalars().first()
        if expert:
            camp_stmt = select(Campaign).where(Campaign.voice_agent_id == expert.id).order_by(desc(Campaign.created_at)).limit(1)
            campaign = (await db.execute(camp_stmt)).scalars().first()

            return {
                "entity_type": "EXPERT",
                "campaign_id": campaign.id if campaign else None,
                "business_id": expert.business_id,
                "campaign": campaign,
                "ai_agent": None,
                "expert": expert,
                "lead_group": None,
                "owner_name": expert.name or "Voice Agent",
                "source_name": "Voice Agent Directory",
                "phone_number": expert.phone or (campaign.twilio_number if campaign else "—"),
                "agent_type": "Voice Agent",
                "status": expert.status or "AVAILABLE",
                "name": expert.name,
                "created_at": expert.created_at,
                "last_updated_at": expert.updated_at,
                "system_prompt": None,
                "voice_id": None,
                "language": "en",
            }

        return None

    @staticmethod
    def validate_tenant_access(agent_ctx: Dict[str, Any], current_user: Optional[User]) -> None:
        """Enforces multi-tenant organizational access control (403 Forbidden)."""
        if not current_user:
            return  # Allow optional auth for unauthenticated preview if configured

        role = (current_user.role or "USER").upper()
        if role == "SUPER_ADMIN":
            return

        user_bid = current_user.business_id
        entity_bid = agent_ctx.get("business_id")

        if user_bid and entity_bid and str(user_bid) != str(entity_bid):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You do not have permission to access this agent's dashboard.",
            )

    @classmethod
    def _build_sql_conditions(
        cls,
        agent_ctx: Dict[str, Any],
        filters: DashboardFiltersQuery,
    ) -> List[Any]:
        conditions = []

        # Scoping conditions
        campaign_id = agent_ctx.get("campaign_id")
        agent_id = agent_ctx.get("ai_agent").id if agent_ctx.get("ai_agent") else None
        expert_id = agent_ctx.get("expert").id if agent_ctx.get("expert") else None

        id_matches = []
        if campaign_id:
            id_matches.append(Call.campaign_id == campaign_id)
        if agent_id:
            id_matches.append(Call.agent_id == agent_id)
        if expert_id:
            id_matches.append(Call.agent_id == expert_id)

        if id_matches:
            conditions.append(or_(*id_matches))

        # Date range
        start_dt, end_dt = _parse_dates(filters)
        if start_dt:
            conditions.append(Call.created_at >= start_dt)
        if end_dt:
            conditions.append(Call.created_at <= end_dt)

        # Statuses
        if filters.status:
            statuses = [s.upper() for s in filters.status if s.upper() not in ("ALL", "")]
            if statuses:
                status_or = []
                for st in statuses:
                    if st == "CONNECTED":
                        status_or.append(or_(
                            Call.status.in_(["IN_PROGRESS", "COMPLETED", "AI_HANDLING", "HUMAN_HANDLING", "QUALIFIED"]),
                            Call.connected_at.is_not(None),
                            Call.duration_seconds > 0,
                        ))
                    elif st in ("FAILED", "ERROR"):
                        status_or.append(Call.status.in_(["FAILED", "ERROR"]))
                    elif st == "BUSY":
                        status_or.append(Call.status == "BUSY")
                    elif st in ("NO_ANSWER", "NOT_ANSWERED"):
                        status_or.append(Call.status.in_(["NO_ANSWER", "RINGING"]))
                    elif st in ("IN_PROGRESS", "IN-PROGRESS"):
                        status_or.append(Call.status.in_(["IN_PROGRESS", "AI_HANDLING", "HUMAN_HANDLING"]))
                    else:
                        status_or.append(Call.status == st)
                if status_or:
                    conditions.append(or_(*status_or))

        # Dispositions / Outcomes
        if filters.disposition:
            disps = [d for d in filters.disposition if d.upper() not in ("ALL", "")]
            if disps:
                disp_or = [func.lower(Call.call_result).contains(escape_like(d.lower())) for d in disps]
                conditions.append(or_(*disp_or))

        # Duration range
        if filters.duration_min is not None and filters.duration_min > 0:
            conditions.append(Call.duration_seconds >= filters.duration_min)
        if filters.duration_max is not None and filters.duration_max > 0:
            conditions.append(Call.duration_seconds <= filters.duration_max)

        # Retry count
        if filters.retry_count is not None and filters.retry_count > 0:
            conditions.append(Call.turn_count >= filters.retry_count)

        # Search keyword
        if filters.search and filters.search.strip():
            kw = f"%{escape_like(filters.search.strip().lower())}%"
            conditions.append(
                or_(
                    func.lower(Call.client_name).contains(kw),
                    func.lower(Call.to_number).contains(kw),
                    func.lower(Call.from_number).contains(kw),
                    func.lower(Call.call_sid).contains(kw),
                    func.lower(Call.transcript).contains(kw),
                )
            )

        return conditions

    @classmethod
    async def get_kpis_and_summary_cached(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        filters: DashboardFiltersQuery,
    ) -> Dict[str, Any]:
        """Calculates KPI summary with SQL aggregation and short 15s in-memory caching."""
        is_pg = _IS_POSTGRES
        cache_key = f"{agent_ctx.get('name')}::{filters.model_dump_json()}"
        cached = _SUMMARY_CACHE.get(cache_key)
        if cached:
            ts, data = cached
            if time.time() - ts < CACHE_TTL_SECONDS:
                return data

        conds = cls._build_sql_conditions(agent_ctx, filters)

        # SQL Aggregate KPI query
        kpi_stmt = (
            select(
                func.count(Call.id).label("total_calls"),
                func.count(
                    case(
                        (or_(Call.connected_at.is_not(None), Call.duration_seconds > 0, Call.status.in_(["IN_PROGRESS", "COMPLETED", "AI_HANDLING", "QUALIFIED"])), 1),
                        else_=None,
                    )
                ).label("connected_calls"),
                func.count(
                    case(
                        (or_(Call.connected_at.is_not(None), Call.duration_seconds > 0), 1),
                        else_=None,
                    )
                ).label("answered_calls"),
                func.count(
                    case(
                        (Call.status.in_(["FAILED", "NO_ANSWER", "BUSY", "ERROR"]), 1),
                        else_=None,
                    )
                ).label("failed_calls"),
                func.avg(
                    case(
                        (Call.duration_seconds > 0, Call.duration_seconds),
                        else_=None,
                    )
                ).label("avg_duration"),
                func.count(
                    case(
                        (or_(
                            func.lower(Call.call_result).contains("qualif"),
                            func.lower(Call.call_result).contains("interest"),
                            func.lower(Call.call_result).contains("success"),
                            func.lower(Call.call_result).contains("callback"),
                        ), 1),
                        else_=None,
                    )
                ).label("success_calls"),
            )
        )
        if conds:
            kpi_stmt = kpi_stmt.where(and_(*conds))

        kpi_row = (await db.execute(kpi_stmt)).one()
        total_calls = kpi_row.total_calls or 0
        connected_calls = kpi_row.connected_calls or 0
        answered_calls = kpi_row.answered_calls or 0
        failed_calls = kpi_row.failed_calls or 0
        avg_dur_sec = int(kpi_row.avg_duration or 0)
        success_calls = kpi_row.success_calls or 0

        mins = avg_dur_sec // 60
        secs = avg_dur_sec % 60
        avg_dur_fmt = f"{mins:02d}:{secs:02d}"
        success_rate = round((success_calls / total_calls * 100), 1) if total_calls > 0 else 0.0

        # Leads in queue & completed count via SQL aggregation
        campaign_id = agent_ctx.get("campaign_id")
        leads_in_queue = 0
        leads_completed = 0
        if campaign_id:
            q_stmt = select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == campaign_id,
                CampaignLead.status.in_(["PENDING", "RESERVED", "DIALING", "RINGING", "RETRY_SCHEDULED"]),
            )
            leads_in_queue = (await db.execute(q_stmt)).scalar() or 0

            comp_stmt = select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == campaign_id,
                CampaignLead.status == "COMPLETED",
            )
            leads_completed = (await db.execute(comp_stmt)).scalar() or 0

        # Outcome distribution aggregation
        outcome_expr = func.coalesce(Call.call_result, "Completed")
        outcome_stmt = (
            select(
                outcome_expr.label("outcome"),
                func.count(Call.id).label("count"),
            )
        )
        if conds:
            outcome_stmt = outcome_stmt.where(and_(*conds))
        outcome_stmt = outcome_stmt.group_by(outcome_expr)
        outcome_rows = (await db.execute(outcome_stmt)).all()
        outcome_dist = {str(row.outcome): row.count for row in outcome_rows if row.outcome}

        # Daily Trend Aggregation (DB-agnostic: works on SQLite and PostgreSQL)
        date_expr = _date_trunc_day(Call.created_at, is_pg)
        trend_stmt = (
            select(
                date_expr.label("d_label"),
                func.count(Call.id).label("total"),
                func.count(case((or_(Call.connected_at.is_not(None), Call.duration_seconds > 0), 1), else_=None)).label("success"),
            )
        )
        if conds:
            trend_stmt = trend_stmt.where(and_(*conds))
        trend_stmt = trend_stmt.group_by(date_expr).order_by(asc(date_expr))
        trend_rows = (await db.execute(trend_stmt)).all()
        call_volume_trend = [
            {"label": str(r.d_label or "Today"), "value": r.total, "secondary_value": r.success}
            for r in trend_rows
        ]

        # Hourly Heatmap Aggregation (DB-agnostic)
        hour_expr = _date_trunc_hour(Call.created_at, is_pg)
        dow_expr = _date_trunc_dow(Call.created_at, is_pg)  # 0=Sunday
        hourly_stmt = (
            select(
                hour_expr.label("hr"),
                dow_expr.label("dow"),
                func.count(Call.id).label("cnt"),
            )
        )
        if conds:
            hourly_stmt = hourly_stmt.where(and_(*conds))
        hourly_stmt = hourly_stmt.group_by(hour_expr, dow_expr)
        hourly_rows = (await db.execute(hourly_stmt)).all()
        matrix = {(r.hr, (r.dow - 1) % 7): r.cnt for r in hourly_rows if r.hr is not None and r.dow is not None}
        hourly_activity = [
            {"hour": h, "day_of_week": d, "count": matrix.get((h, d), 0)}
            for d in range(7)
            for h in range(24)
        ]

        result = {
            "kpis": {
                "total_calls": total_calls,
                "connected_calls": connected_calls,
                "answered_calls": answered_calls,
                "missed_failed_calls": failed_calls,
                "avg_call_duration_seconds": avg_dur_sec,
                "avg_call_duration_formatted": avg_dur_fmt,
                "success_rate_percentage": success_rate,
                "leads_in_queue": leads_in_queue,
                "leads_completed": leads_completed,
                "cost_metric": {
                    "available": False,
                    "amount": None,
                    "reason": "Per-call cost credits are not recorded on call records in database.",
                },
                "total_cost_credits": None,
            },
            "outcome_distribution": outcome_dist,
            "hourly_activity": hourly_activity,
            "call_volume_trend": call_volume_trend,
        }

        _SUMMARY_CACHE[cache_key] = (time.time(), result)
        return result

    @classmethod
    async def get_calls_page(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        filters: DashboardFiltersQuery,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """Returns paginated, eager-loaded calls matching filters."""
        conds = cls._build_sql_conditions(agent_ctx, filters)
        query = select(Call, Lead).outerjoin(Lead, Call.lead_id == Lead.id)
        if conds:
            query = query.where(and_(*conds))

        # Count total
        count_query = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_query)).scalar() or 0

        # Sort order
        order_col = Call.created_at
        if filters.sort_by == "duration":
            order_col = Call.duration_seconds
        elif filters.sort_by == "status":
            order_col = Call.status

        if (filters.sort_order or "").lower() == "asc":
            query = query.order_by(asc(order_col))
        else:
            query = query.order_by(desc(order_col))

        # Pagination with cap at 100
        page_size = min(max(1, filters.page_size), 100)
        offset = max(0, (filters.page - 1) * page_size)
        query = query.offset(offset).limit(page_size)

        rows = (await db.execute(query)).all()
        items = []

        for call_row, lead_row in rows:
            dur = call_row.duration_seconds or 0
            dur_fmt = f"{dur // 60:02d}:{dur % 60:02d}"

            lead_name = call_row.client_name
            if lead_row:
                name_str = f"{lead_row.first_name or ''} {lead_row.last_name or ''}".strip()
                lead_name = name_str or lead_row.email or lead_name
            if not lead_name:
                lead_name = "Unknown Lead"

            phone = call_row.to_number or (lead_row.phone if lead_row else "") or "—"

            # Derive sentiment
            res = (call_row.call_result or "").lower()
            sent = "NEUTRAL"
            if any(w in res for w in ("qualif", "interest", "positive", "won")):
                sent = "POSITIVE"
            elif any(w in res for w in ("dnc", "not interest", "refused", "hangup", "angry")):
                sent = "NEGATIVE"

            items.append({
                "call_id": call_row.id,
                "lead_id": call_row.lead_id,
                "lead_name": lead_name,
                "phone_number": phone,
                "start_time": call_row.initiated_at or call_row.created_at,
                "duration_seconds": dur,
                "duration_formatted": dur_fmt,
                "status": call_row.status or "COMPLETED",
                "outcome": call_row.call_result or "—",
                "disposition": call_row.call_result or "—",
                "sentiment": sent,
                "recording_url": call_row.recording_url or None,
                "transcript": call_row.transcript or None,
                "ai_summary": f"Call ended with status '{call_row.status}'. Disposition: {call_row.call_result or 'Standard completion'}.",
                "retry_count": call_row.turn_count or 0,
            })

        return items, total

    @classmethod
    async def get_call_by_id(
        cls,
        agent_ctx: Dict[str, Any],
        call_id: str,
        db: AsyncSession,
    ) -> Optional[Dict[str, Any]]:
        """Retrieves a single call with full transcript dialogue for drawer."""
        stmt = (
            select(Call, Lead)
            .outerjoin(Lead, Call.lead_id == Lead.id)
            .where(Call.id == call_id)
        )
        res = (await db.execute(stmt)).first()
        if not res:
            return None

        call_row, lead_row = res
        dur = call_row.duration_seconds or 0
        dur_fmt = f"{dur // 60:02d}:{dur % 60:02d}"

        lead_name = call_row.client_name
        if lead_row:
            name_str = f"{lead_row.first_name or ''} {lead_row.last_name or ''}".strip()
            lead_name = name_str or lead_row.email or lead_name
        if not lead_name:
            lead_name = "Unknown Lead"

        phone = call_row.to_number or (lead_row.phone if lead_row else "") or "—"

        return {
            "call_id": call_row.id,
            "campaign_id": call_row.campaign_id,
            "agent_id": call_row.agent_id,
            "agent_type": call_row.agent_type or "AI_VOICE",
            "lead_id": call_row.lead_id,
            "lead_name": lead_name,
            "phone_number": phone,
            "start_time": call_row.initiated_at or call_row.created_at,
            "end_time": call_row.ended_at,
            "duration_seconds": dur,
            "duration_formatted": dur_fmt,
            "status": call_row.status or "COMPLETED",
            "disposition": call_row.call_result or "—",
            "sentiment": "NEUTRAL",
            "recording_url": call_row.recording_url or None,
            "transcript": call_row.transcript or None,
            "ai_summary": f"Call duration {dur}s. Hangup: {call_row.hangup_reason or 'Normal'}.",
            "turn_count": call_row.turn_count or 0,
            "hangup_reason": call_row.hangup_reason,
            "metadata": {
                "call_sid": call_row.call_sid,
                "direction": call_row.direction,
            },
        }

    @classmethod
    async def search_transcripts_keyword(
        cls,
        agent_ctx: Dict[str, Any],
        query_str: str,
        db: AsyncSession,
    ) -> List[Dict[str, Any]]:
        """Searches transcripts with keyword extraction and snippet generation."""
        conds = []
        campaign_id = agent_ctx.get("campaign_id")
        if campaign_id:
            conds.append(Call.campaign_id == campaign_id)

        clean_kw = escape_like(query_str.strip().lower())
        conds.append(func.lower(Call.transcript).contains(clean_kw))

        stmt = (
            select(Call, Lead)
            .outerjoin(Lead, Call.lead_id == Lead.id)
            .where(and_(*conds))
            .order_by(desc(Call.created_at))
            .limit(50)
        )
        rows = (await db.execute(stmt)).all()

        results = []
        for c, l in rows:
            text = c.transcript or ""
            idx = text.lower().find(query_str.lower())
            snippet = text
            if idx >= 0:
                start = max(0, idx - 40)
                end = min(len(text), idx + len(query_str) + 60)
                prefix = "..." if start > 0 else ""
                suffix = "..." if end < len(text) else ""
                snippet = f"{prefix}{text[start:end]}{suffix}"

            dur = c.duration_seconds or 0
            dur_fmt = f"{dur // 60:02d}:{dur % 60:02d}"

            name = f"{l.first_name or ''} {l.last_name or ''}".strip() if l else c.client_name
            results.append({
                "call_id": c.id,
                "lead_name": name or "Unknown Lead",
                "phone_number": c.to_number or "—",
                "start_time": c.initiated_at or c.created_at,
                "duration_formatted": dur_fmt,
                "matched_snippet": snippet,
                "full_transcript": text,
                "recording_url": c.recording_url or None,
                "sentiment": "NEUTRAL",
                "outcome": c.call_result or "—",
            })

        return results

    @classmethod
    async def get_filter_options_data(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
    ) -> Dict[str, Any]:
        """Provides dynamic filter dropdown values from database."""
        campaign_id = agent_ctx.get("campaign_id")
        conds = [Call.campaign_id == campaign_id] if campaign_id else []

        # Distinct statuses
        status_stmt = select(Call.status, func.count(Call.id)).where(and_(*conds)).group_by(Call.status)
        status_rows = (await db.execute(status_stmt)).all()
        statuses = [{"label": str(r[0]), "value": str(r[0]), "count": r[1]} for r in status_rows if r[0]]

        # Distinct dispositions
        disp_stmt = select(Call.call_result, func.count(Call.id)).where(and_(*conds)).group_by(Call.call_result)
        disp_rows = (await db.execute(disp_stmt)).all()
        dispositions = [{"label": str(r[0]), "value": str(r[0]), "count": r[1]} for r in disp_rows if r[0] and r[0] != "—"]

        # Lead libraries
        bid = agent_ctx.get("business_id")
        lg_stmt = select(LeadGroup).where(LeadGroup.business_id == bid) if bid else select(LeadGroup)
        lead_groups = (await db.execute(lg_stmt)).scalars().all()
        lead_libs = [
            {
                "label": getattr(lg, "filename", None) or getattr(lg, "name", lg.id),
                "value": lg.id,
                "count": getattr(lg, "lead_count", 0),
            }
            for lg in lead_groups
        ]

        sentiments = [
            {"label": "Positive", "value": "POSITIVE", "count": None},
            {"label": "Neutral", "value": "NEUTRAL", "count": None},
            {"label": "Negative", "value": "NEGATIVE", "count": None},
        ]

        return {
            "statuses": statuses,
            "dispositions": dispositions,
            "lead_libraries": lead_libs,
            "sentiments": sentiments,
        }

    @classmethod
    async def get_queue_page(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        status: Optional[str] = None,
        search: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        campaign_id = agent_ctx.get("campaign_id")
        if not campaign_id:
            return [], 0

        query = (
            select(CampaignLead, Lead)
            .join(Lead, CampaignLead.lead_id == Lead.id)
            .where(CampaignLead.campaign_id == campaign_id)
        )

        if status and status.upper() not in ("ALL", ""):
            query = query.where(CampaignLead.status == status.upper())

        if search and search.strip():
            st = f"%{escape_like(search.strip().lower())}%"
            query = query.where(
                or_(
                    func.lower(Lead.first_name).contains(st),
                    func.lower(Lead.last_name).contains(st),
                    func.lower(Lead.phone).contains(st),
                    func.lower(Lead.email).contains(st),
                )
            )

        count_query = select(func.count()).select_from(query.subquery())
        total = (await db.execute(count_query)).scalar() or 0

        page_size = min(max(1, page_size), 100)
        offset = max(0, (page - 1) * page_size)
        query = query.order_by(asc(CampaignLead.created_at)).offset(offset).limit(page_size)

        rows = (await db.execute(query)).all()
        items = []
        for idx, (cl, l) in enumerate(rows):
            name = f"{l.first_name or ''} {l.last_name or ''}".strip() or l.email or "Lead"
            items.append({
                "id": cl.id,
                "lead_id": cl.lead_id,
                "lead_name": name,
                "phone_number": l.phone or "—",
                "position": offset + idx + 1,
                "scheduled_time": cl.created_at,
                "attempts": cl.attempt_count or 0,
                "next_retry_time": cl.next_retry_at,
                "status": cl.status or "PENDING",
            })

        return items, total

    @classmethod
    async def get_history_page(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        campaign_id = agent_ctx.get("campaign_id")
        history_list = []

        if campaign_id:
            audit_stmt = (
                select(AuditLog)
                .where(
                    or_(
                        AuditLog.entity_id == campaign_id,
                        AuditLog.entity == "campaign",
                    )
                )
                .order_by(desc(AuditLog.created_at))
                .limit(50)
            )
            audits = (await db.execute(audit_stmt)).scalars().all()
            for a in audits:
                history_list.append({
                    "id": a.id,
                    "timestamp": a.created_at,
                    "event_type": a.action or "AUDIT_EVENT",
                    "title": f"{(a.action or 'audit').replace('_', ' ').capitalize()} Campaign",
                    "description": f"Audit record for campaign resource: {a.action}",
                    "actor": a.user_id or "Admin",
                    "details": a.details if isinstance(a.details, dict) else {},
                    "badge_color": "indigo",
                })

        camp = agent_ctx.get("campaign")
        if camp:
            if camp.created_at:
                history_list.append({
                    "id": f"evt_created_{camp.id}",
                    "timestamp": camp.created_at,
                    "event_type": "CAMPAIGN_CREATED",
                    "title": f"Campaign '{camp.name}' created",
                    "description": f"Campaign initialized with agent type {agent_ctx.get('agent_type')}",
                    "actor": agent_ctx.get("owner_name") or "System",
                    "details": {"status": "IDLE"},
                    "badge_color": "blue",
                })
            if camp.started_at:
                history_list.append({
                    "id": f"evt_started_{camp.id}",
                    "timestamp": camp.started_at,
                    "event_type": "CAMPAIGN_STARTED",
                    "title": f"Campaign '{camp.name}' started",
                    "description": "Dialer queue started outbound calls",
                    "actor": "Supervisor",
                    "details": {"status": "RUNNING"},
                    "badge_color": "emerald",
                })
            if camp.completed_at:
                history_list.append({
                    "id": f"evt_completed_{camp.id}",
                    "timestamp": camp.completed_at,
                    "event_type": "CAMPAIGN_COMPLETED",
                    "title": f"Campaign '{camp.name}' completed",
                    "description": "All leads processed in queue",
                    "actor": "System Automation",
                    "details": {"status": "COMPLETED"},
                    "badge_color": "purple",
                })

        history_list.sort(key=lambda x: x["timestamp"] or datetime.min, reverse=True)
        total = len(history_list)
        page_size = min(max(1, page_size), 100)
        offset = max(0, (page - 1) * page_size)
        paged = history_list[offset:offset + page_size]

        return paged, total

    @classmethod
    async def get_reports_aggregation(
        cls,
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        granularity: str = "day",
        filters: Optional[DashboardFiltersQuery] = None,
    ) -> Dict[str, Any]:
        conds = cls._build_sql_conditions(agent_ctx, filters or DashboardFiltersQuery())

        is_pg = _IS_POSTGRES

        if granularity == "week":
            date_fmt = _date_fmt_week(Call.created_at, is_pg)
        elif granularity == "month":
            date_fmt = _date_fmt_month(Call.created_at, is_pg)
        else:
            date_fmt = _date_trunc_day(Call.created_at, is_pg)

        report_stmt = (
            select(
                date_fmt.label("bucket"),
                func.count(Call.id).label("total"),
                func.count(case((or_(Call.connected_at.is_not(None), Call.duration_seconds > 0), 1), else_=None)).label("connected"),
                func.count(case((Call.status.in_(["FAILED", "NO_ANSWER", "BUSY", "ERROR"]), 1), else_=None)).label("failed"),
                func.avg(case((Call.duration_seconds > 0, Call.duration_seconds), else_=None)).label("avg_dur"),
            )
        )
        if conds:
            report_stmt = report_stmt.where(and_(*conds))
        report_stmt = report_stmt.group_by(date_fmt).order_by(asc(date_fmt))

        rows = (await db.execute(report_stmt)).all()
        items = []
        tot_all = 0
        conn_all = 0

        for r in rows:
            t = r.total or 0
            c = r.connected or 0
            f = r.failed or 0
            dur = round(r.avg_dur or 0, 1)
            succ = round((c / t * 100), 1) if t > 0 else 0.0

            tot_all += t
            conn_all += c

            items.append({
                "date_label": str(r.bucket or "Period"),
                "total_calls": t,
                "connected_calls": c,
                "failed_calls": f,
                "avg_duration_seconds": dur,
                "success_rate_percentage": succ,
            })

        overall_succ = round((conn_all / tot_all * 100), 1) if tot_all > 0 else 0.0

        return {
            "granularity": granularity,
            "items": items,
            "total_calls": tot_all,
            "overall_success_rate": overall_succ,
        }
