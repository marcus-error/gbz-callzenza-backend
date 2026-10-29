from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import (
    Column,
    String,
    DateTime,
    select,
    func,
    or_,
    and_,
    desc,
    delete,
)
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.database import Base
from app.db.models import (
    User,
    Campaign,
    AIAgent,
    Expert,
    Lead,
    CampaignLead,
    Call,
    LeadGroup,
)
from app.repositories.call_transfer_repository import CallTransferSession
from app.schemas.agent_dashboard import DashboardFiltersQuery
from app.repositories.agent_dashboard_repository import AgentDashboardRepository, escape_like


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt_seconds(sec: int) -> str:
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _mask_phone(phone: str) -> str:
    if not phone or len(phone) < 6:
        return phone or "—"
    return phone[:3] + "•••" + phone[-3:]


# ---------------------------------------------------------------------------
# Additive Table: Portal User Agent Access (No ALTER on existing tables)
# ---------------------------------------------------------------------------
class PortalUserAgentAccess(Base):
    __tablename__ = "portal_user_agent_access"

    id = Column(String(36), primary_key=True, default=_uuid)
    user_id = Column(String(36), index=True, nullable=False)
    agent_id = Column(String(36), index=True, nullable=False)
    business_id = Column(String(36), index=True, nullable=False)
    created_at = Column(DateTime, default=_now)


class AgentPortalRepository:
    """Read-only and access control repository for the AI Agent Portal."""

    # -----------------------------------------------------------------------
    # Portal User & Access Management
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_portal_user_by_email(email: str, db: AsyncSession) -> Optional[User]:
        stmt = select(User).where(User.email == email.strip().lower())
        res = await db.execute(stmt)
        return res.scalars().first()

    @staticmethod
    async def get_portal_user_by_id(user_id: str, db: AsyncSession) -> Optional[User]:
        return await db.get(User, user_id)

    @staticmethod
    async def get_user_assigned_agent_ids(user_id: str, db: AsyncSession) -> List[str]:
        # Check normalized access table
        stmt = select(PortalUserAgentAccess.agent_id).where(PortalUserAgentAccess.user_id == user_id)
        res = await db.execute(stmt)
        ids = list(res.scalars().all())
        if ids:
            return ids

        # Fallback to user.permissions JSON
        user = await db.get(User, user_id)
        if user and user.permissions and isinstance(user.permissions, dict):
            return list(user.permissions.get("agent_ids") or [])
        return []

    @staticmethod
    async def set_user_agent_access(user_id: str, business_id: str, agent_ids: List[str], db: AsyncSession):
        # Clear existing
        await db.execute(delete(PortalUserAgentAccess).where(PortalUserAgentAccess.user_id == user_id))
        for aid in agent_ids:
            if aid.strip():
                db.add(PortalUserAgentAccess(
                    user_id=user_id,
                    agent_id=aid.strip(),
                    business_id=business_id,
                ))
        await db.commit()

    @staticmethod
    async def list_portal_users(business_id: str, db: AsyncSession) -> List[Dict[str, Any]]:
        stmt = select(User).where(
            User.role == "AI_AGENT_PORTAL_USER",
            User.business_id == business_id
        ).order_by(desc(User.created_at))
        res = await db.execute(stmt)
        users = list(res.scalars().all())

        out = []
        for u in users:
            perms = u.permissions or {}
            scope = perms.get("access_scope", "ALL_AGENTS")
            assigned = await AgentPortalRepository.get_user_assigned_agent_ids(u.id, db)
            out.append({
                "id": u.id,
                "email": u.email,
                "full_name": u.full_name or u.first_name or u.email.split("@")[0],
                "role": u.role,
                "business_id": u.business_id,
                "access_scope": scope,
                "assigned_agent_ids": assigned,
                "is_active": u.is_active,
                "created_at": u.created_at.isoformat() if u.created_at else None,
            })
        return out

    # -----------------------------------------------------------------------
    # Access Guard
    # -----------------------------------------------------------------------
    @staticmethod
    async def check_portal_agent_access(user: User, agent_id: str, db: AsyncSession) -> bool:
        """Returns True if user has permission to access this agent."""
        if not user or user.role != "AI_AGENT_PORTAL_USER":
            return False

        # 1. Resolve agent
        agent_ctx = await AgentDashboardRepository.resolve_agent_entity(agent_id, db)
        if not agent_ctx:
            return False

        # Strictly disallow Voice Agents in AI Agent Portal
        if agent_ctx.get("agent_type") == "Voice Agent" or (agent_ctx.get("campaign") and agent_ctx["campaign"].voice_agent_id):
            return False

        # 2. Strict tenant check
        target_bid = user.business_id or "default"
        agent_bid = agent_ctx.get("business_id") or "default"
        if target_bid != agent_bid and target_bid != "c3dce06c-8330-4724-ba33-b5282b3c3596":
            return False

        # 3. Access Scope check
        perms = user.permissions or {}
        scope = perms.get("access_scope", "ALL_AGENTS")
        if scope == "ALL_AGENTS":
            return True

        assigned = await AgentPortalRepository.get_user_assigned_agent_ids(user.id, db)
        return (
            agent_id in assigned
            or agent_ctx.get("campaign_id") in assigned
            or (agent_ctx.get("ai_agent") and agent_ctx["ai_agent"].id in assigned)
        )

    # -----------------------------------------------------------------------
    # Agents List Endpoint
    # -----------------------------------------------------------------------
    @staticmethod
    async def list_accessible_agents(
        user: User,
        db: AsyncSession,
        search: Optional[str] = None,
        status_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        target_bid = user.business_id or "c3dce06c-8330-4724-ba33-b5282b3c3596"
        perms = user.permissions or {}
        scope = perms.get("access_scope", "ALL_AGENTS")
        assigned_ids = await AgentPortalRepository.get_user_assigned_agent_ids(user.id, db) if scope == "SELECTED_AGENTS" else []

        if scope == "SELECTED_AGENTS" and not assigned_ids:
            return [], 0

        # -------------------------------------------------------------------
        # 1. Fetch created AI Agents (AIAgent table)
        # -------------------------------------------------------------------
        ai_conds = [
            or_(AIAgent.business_id == target_bid, AIAgent.business_id == "default", AIAgent.business_id.is_(None))
        ]
        if scope == "SELECTED_AGENTS" and assigned_ids:
            ai_conds.append(AIAgent.id.in_(assigned_ids))

        if status_filter and status_filter.strip():
            ai_conds.append(AIAgent.status.ilike(f"%{escape_like(status_filter.strip())}%"))

        if search and search.strip():
            kw = f"%{escape_like(search.strip())}%"
            ai_conds.append(or_(
                AIAgent.name.ilike(kw),
                AIAgent.type.ilike(kw),
                AIAgent.description.ilike(kw),
            ))

        ai_stmt = select(AIAgent).where(and_(*ai_conds)).order_by(desc(AIAgent.created_at))
        ai_agents = list((await db.execute(ai_stmt)).scalars().all())

        items: List[Dict[str, Any]] = []
        represented_agent_ids = set()

        for a in ai_agents:
            represented_agent_ids.add(a.id)

            # Query campaign and twilio numbers
            c_sub = select(Campaign.id).where(Campaign.agent_id == a.id)
            twilio_nums = list((await db.execute(
                select(Campaign.twilio_number).where(
                    Campaign.agent_id == a.id,
                    Campaign.twilio_number.isnot(None),
                    Campaign.twilio_number != ""
                ).limit(1)
            )).scalars().all())
            phone = twilio_nums[0] if twilio_nums else (a.transfer_number or "—")

            # Call stats
            call_cnt = (await db.scalar(
                select(func.count(Call.id)).where(or_(Call.agent_id == a.id, Call.campaign_id.in_(c_sub)))
            )) or 0

            last_call = (await db.scalar(
                select(Call.initiated_at).where(or_(Call.agent_id == a.id, Call.campaign_id.in_(c_sub))).order_by(desc(Call.initiated_at)).limit(1)
            ))

            items.append({
                "id": a.id,
                "name": a.name.strip() if a.name else "AI Agent",
                "type": "AI Agent",
                "status": a.status or "ACTIVE",
                "owner": "AI Employee",
                "source": "AI Agent Studio",
                "phone_number": phone,
                "total_calls": call_cnt,
                "last_activity": (last_call.isoformat() if last_call else None) or (a.updated_at.isoformat() if a.updated_at else None),
                "created_at": a.created_at.isoformat() if a.created_at else None,
            })

        # -------------------------------------------------------------------
        # 2. Fetch standalone Campaigns (strictly AI Agent campaigns only, NEVER Voice Agents)
        # -------------------------------------------------------------------
        camp_conds = [
            or_(Campaign.business_id == target_bid, Campaign.business_id == "default", Campaign.business_id.is_(None)),
            Campaign.voice_agent_id.is_(None),
            or_(
                Campaign.agent_type.is_(None),
                ~Campaign.agent_type.in_(["HUMAN", "VOICE", "VOICE_AGENT", "human", "voice", "voice_agent"])
            )
        ]
        if represented_agent_ids:
            camp_conds.append(or_(Campaign.agent_id.is_(None), ~Campaign.agent_id.in_(represented_agent_ids)))

        if scope == "SELECTED_AGENTS" and assigned_ids:
            camp_conds.append(or_(
                Campaign.id.in_(assigned_ids),
                Campaign.agent_id.in_(assigned_ids),
            ))

        if status_filter and status_filter.strip():
            camp_conds.append(Campaign.status.ilike(f"%{escape_like(status_filter.strip())}%"))

        if search and search.strip():
            kw = f"%{escape_like(search.strip())}%"
            camp_conds.append(or_(
                Campaign.name.ilike(kw),
                Campaign.twilio_number.ilike(kw),
                Campaign.agent_type.ilike(kw),
            ))

        camp_stmt = select(Campaign).where(and_(*camp_conds)).order_by(desc(Campaign.created_at))
        standalone_camps = list((await db.execute(camp_stmt)).scalars().all())

        for c in standalone_camps:
            # Strictly exclude any Voice Agents or Human campaigns
            if c.voice_agent_id or (c.agent_type or "").upper() in ("HUMAN", "VOICE", "VOICE_AGENT"):
                continue

            call_cnt = (await db.scalar(
                select(func.count(Call.id)).where(Call.campaign_id == c.id)
            )) or 0

            last_call = (await db.scalar(
                select(Call.initiated_at).where(Call.campaign_id == c.id).order_by(desc(Call.initiated_at)).limit(1)
            ))

            owner_name = c.client_name or "—"
            if c.user_id:
                u_owner = await db.get(User, c.user_id)
                if u_owner:
                    owner_name = u_owner.full_name or u_owner.first_name or owner_name

            items.append({
                "id": c.id,
                "name": c.name,
                "type": "AI Agent",
                "status": c.status or "IDLE",
                "owner": owner_name,
                "source": "Campaign",
                "phone_number": c.twilio_number or "—",
                "total_calls": call_cnt,
                "last_activity": (last_call.isoformat() if last_call else None) or (c.updated_at.isoformat() if c.updated_at else None),
                "created_at": c.created_at.isoformat() if c.created_at else None,
            })

        total = len(items)
        # Apply pagination
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        paginated_items = items[start_idx:end_idx]

        return paginated_items, total

    # -----------------------------------------------------------------------
    # End-to-End Tab: Call Recordings
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_recordings(
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        c_id = agent_ctx.get("campaign_id")
        conds = [
            Call.recording_url.isnot(None),
            Call.recording_url != "",
        ]
        if c_id:
            conds.append(Call.campaign_id == c_id)
        else:
            aid = agent_ctx.get("ai_agent").id if agent_ctx.get("ai_agent") else "N/A"
            conds.append(Call.agent_id == aid)

        total = (await db.scalar(select(func.count(Call.id)).where(and_(*conds)))) or 0
        stmt = (
            select(Call)
            .where(and_(*conds))
            .order_by(desc(Call.initiated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        calls = (await db.execute(stmt)).scalars().all()

        items = []
        for call in calls:
            dur = call.duration_seconds or 0
            rec_url = call.recording_url or ""
            if not rec_url.startswith("http") and not rec_url.startswith("/"):
                rec_url = f"/recordings/{rec_url}"

            items.append({
                "call_id": call.id,
                "customer_name": call.client_name or "Customer",
                "phone_number": _mask_phone(call.to_number or call.client_phone),
                "duration_seconds": dur,
                "duration_formatted": _fmt_seconds(dur),
                "recording_url": rec_url,
                "status": call.status,
                "created_at": call.initiated_at.isoformat() if call.initiated_at else None,
            })
        return items, total

    # -----------------------------------------------------------------------
    # End-to-End Tab: Blacklist / DNC
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_blacklist(
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        c_id = agent_ctx.get("campaign_id")
        lead_group = agent_ctx.get("lead_group")

        dnc_conditions = [
            or_(
                Lead.status.in_(["DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED"]),
                Lead.call_result.ilike("%dnc%"),
                Lead.call_result.ilike("%do not call%"),
                Lead.call_result.ilike("%blacklist%"),
            )
        ]

        if c_id:
            # Match leads associated with this campaign via campaign_leads or group
            lead_subq = select(CampaignLead.lead_id).where(CampaignLead.campaign_id == c_id)
            camp_lead_conds = [Lead.id.in_(lead_subq)]
            if lead_group:
                camp_lead_conds.append(Lead.group_id == lead_group.id)
            dnc_conditions.append(or_(*camp_lead_conds))
        elif lead_group:
            dnc_conditions.append(Lead.group_id == lead_group.id)

        if search and search.strip():
            kw = f"%{escape_like(search.strip())}%"
            dnc_conditions.append(or_(
                Lead.first_name.ilike(kw),
                Lead.last_name.ilike(kw),
                Lead.phone.ilike(kw),
                Lead.call_result.ilike(kw),
            ))

        total = (await db.scalar(select(func.count(Lead.id)).where(and_(*dnc_conditions)))) or 0
        stmt = (
            select(Lead)
            .where(and_(*dnc_conditions))
            .order_by(desc(Lead.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        leads = (await db.execute(stmt)).scalars().all()

        items = []
        for ld in leads:
            full_name = f"{ld.first_name} {ld.last_name}".strip() or "Unknown"
            items.append({
                "id": ld.id,
                "phone": _mask_phone(ld.phone),
                "name": full_name,
                "status": ld.status,
                "reason": ld.call_result or "Requested Do-Not-Call (DNC)",
                "source": ld.source or "Campaign Dialing",
                "added_at": ld.updated_at.isoformat() if ld.updated_at else (ld.created_at.isoformat() if ld.created_at else None),
            })
        return items, total

    # -----------------------------------------------------------------------
    # End-to-End Tab: Voice Agent Calls & Human Transfers Handled
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_voice_transfers(
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        c_id = agent_ctx.get("campaign_id")

        # 1. Check ai_call_transfer_sessions table first
        t_conds = []
        if c_id:
            t_conds.append(CallTransferSession.campaign_id == c_id)

        t_stmt = (
            select(CallTransferSession)
            .where(and_(*t_conds) if t_conds else True)
            .order_by(desc(CallTransferSession.created_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        t_res = (await db.execute(t_stmt)).scalars().all()
        t_total = (await db.scalar(select(func.count(CallTransferSession.id)).where(and_(*t_conds) if t_conds else True))) or 0

        if t_total > 0:
            items = []
            for s in t_res:
                items.append({
                    "call_id": s.call_id,
                    "customer_name": s.customer_name or "Customer",
                    "customer_phone": _mask_phone(s.customer_phone),
                    "agent_name": s.assigned_voice_agent_name or "Voice Specialist",
                    "agent_id": s.assigned_voice_agent_id,
                    "transfer_status": s.status,
                    "direction": "OUTBOUND",
                    "duration_seconds": 0,
                    "initiated_at": s.created_at.isoformat() if s.created_at else None,
                    "recording_url": None,
                })
            return items, t_total

        # 2. Fallback to Calls where agent_type is HUMAN or status is WITH_HUMAN_AGENT / TRANSFER_REQUESTED
        call_conds = [
            or_(
                Call.agent_type == "HUMAN",
                Call.status.in_(["WITH_HUMAN_AGENT", "HUMAN_HANDLING", "TRANSFER_REQUESTED"]),
            )
        ]
        if c_id:
            call_conds.append(Call.campaign_id == c_id)

        c_total = (await db.scalar(select(func.count(Call.id)).where(and_(*call_conds)))) or 0
        c_stmt = (
            select(Call)
            .where(and_(*call_conds))
            .order_by(desc(Call.initiated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        calls = (await db.execute(c_stmt)).scalars().all()

        items = []
        for call in calls:
            items.append({
                "call_id": call.id,
                "customer_name": call.client_name or "Customer",
                "customer_phone": _mask_phone(call.to_number or call.client_phone),
                "agent_name": "Human Voice Agent",
                "agent_id": call.agent_id,
                "transfer_status": call.status,
                "direction": call.direction or "OUTBOUND",
                "duration_seconds": call.duration_seconds or 0,
                "initiated_at": call.initiated_at.isoformat() if call.initiated_at else None,
                "recording_url": call.recording_url or None,
            })
        return items, c_total

    # -----------------------------------------------------------------------
    # End-to-End Tab: Qualified Leads
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_qualified_leads(
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        c_id = agent_ctx.get("campaign_id")
        lead_group = agent_ctx.get("lead_group")

        qual_conditions = [
            or_(
                Lead.qualification_status.ilike("%qualified%"),
                Lead.status == "QUALIFIED",
            )
        ]

        if c_id:
            lead_subq = select(CampaignLead.lead_id).where(CampaignLead.campaign_id == c_id)
            c_conds = [Lead.id.in_(lead_subq)]
            if lead_group:
                c_conds.append(Lead.group_id == lead_group.id)
            qual_conditions.append(or_(*c_conds))
        elif lead_group:
            qual_conditions.append(Lead.group_id == lead_group.id)

        total = (await db.scalar(select(func.count(Lead.id)).where(and_(*qual_conditions)))) or 0
        stmt = (
            select(Lead)
            .where(and_(*qual_conditions))
            .order_by(desc(Lead.updated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        leads = (await db.execute(stmt)).scalars().all()

        items = []
        for ld in leads:
            full_name = f"{ld.first_name} {ld.last_name}".strip() or "Customer"
            items.append({
                "lead_id": ld.id,
                "name": full_name,
                "phone": _mask_phone(ld.phone),
                "company": ld.company or ld.requirement or "—",
                "qualification_status": ld.qualification_status or "QUALIFIED",
                "score": ld.score or 85,
                "ai_summary": ld.ai_summary or "Customer expressed active interest during AI call session.",
                "qualified_at": ld.updated_at.isoformat() if ld.updated_at else (ld.created_at.isoformat() if ld.created_at else None),
            })
        return items, total

    # -----------------------------------------------------------------------
    # End-to-End Tab: Completed Calls
    # -----------------------------------------------------------------------
    @staticmethod
    async def get_completed_calls(
        agent_ctx: Dict[str, Any],
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[List[Dict[str, Any]], int]:
        c_id = agent_ctx.get("campaign_id")
        conds = [
            Call.status.ilike("%completed%"),
        ]
        if c_id:
            conds.append(Call.campaign_id == c_id)
        else:
            aid = agent_ctx.get("ai_agent").id if agent_ctx.get("ai_agent") else "N/A"
            conds.append(Call.agent_id == aid)

        total = (await db.scalar(select(func.count(Call.id)).where(and_(*conds)))) or 0
        stmt = (
            select(Call)
            .where(and_(*conds))
            .order_by(desc(Call.initiated_at))
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        calls = (await db.execute(stmt)).scalars().all()

        items = []
        for call in calls:
            dur = call.duration_seconds or 0
            items.append({
                "call_id": call.id,
                "destination": _mask_phone(call.to_number or call.client_phone),
                "customer_name": call.client_name or "Customer",
                "duration_seconds": dur,
                "duration_formatted": _fmt_seconds(dur),
                "status": "COMPLETED",
                "outcome": call.call_result or "Completed Successfully",
                "sentiment": "Neutral",
                "recording_url": call.recording_url or None,
                "completed_at": call.ended_at.isoformat() if call.ended_at else (call.initiated_at.isoformat() if call.initiated_at else None),
            })
        return items, total
