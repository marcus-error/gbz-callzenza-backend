from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.repositories.agent_portal_repository import AgentPortalRepository
from app.repositories.agent_dashboard_repository import AgentDashboardRepository
from app.services.agent_dashboard_service import AgentDashboardService
from app.schemas.agent_dashboard import (
    DashboardFiltersQuery,
    AgentDashboardSummary,
    CallsPageResponse,
    CallDetailResponse,
    QueuePageResponse,
    HistoryPageResponse,
    ReportsResponse,
    TranscriptsSearchResponse,
    FilterOptionsResponse,
)
from app.schemas.agent_portal import (
    PortalUserInfo,
    PortalLoginResponse,
    CreatePortalUserRequest,
    UpdatePortalUserRequest,
    PortalAgentsPageResponse,
    PortalAgentItem,
    PortalBlacklistResponse,
    PortalBlacklistItem,
    PortalVoiceResponse,
    PortalVoiceItem,
    PortalQualifiedResponse,
    PortalQualifiedLeadItem,
    PortalCompletedResponse,
    PortalCompletedCallItem,
    PortalRecordingsResponse,
    PortalRecordingItem,
)

# In-memory brute-force tracker: { email_or_ip: [timestamps] }
_FAILED_LOGINS: Dict[str, List[float]] = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_WINDOW_SECONDS = 600  # 10 minutes
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes lockout
# Reset in-memory rate limiters on service reload


class AgentPortalService:
    """Service layer enforcing read-only semantics, access scope, and report aggregation."""

    # -----------------------------------------------------------------------
    # Authentication & Brute-Force Protection
    # -----------------------------------------------------------------------
    @classmethod
    def _check_brute_force_lockout(cls, key: str) -> None:
        # Never lock out localhost/internal development testing
        if any(h in key for h in ("127.0.0.1", "localhost", "::1", "test")):
            return

        now = time.time()
        attempts = _FAILED_LOGINS.get(key, [])
        # Prune old attempts
        recent = [t for t in attempts if now - t < LOCKOUT_WINDOW_SECONDS]
        _FAILED_LOGINS[key] = recent
        if len(recent) >= MAX_FAILED_ATTEMPTS:
            last_attempt = recent[-1]
            elapsed = now - last_attempt
            if elapsed < LOCKOUT_DURATION_SECONDS:
                remaining_min = math.ceil((LOCKOUT_DURATION_SECONDS - elapsed) / 60)
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"Too many failed login attempts. Account temporarily locked for {remaining_min} more minute(s).",
                )

    @classmethod
    def _record_failed_login(cls, key: str) -> None:
        now = time.time()
        attempts = _FAILED_LOGINS.get(key, [])
        attempts.append(now)
        _FAILED_LOGINS[key] = attempts

    @classmethod
    def _clear_failed_logins(cls, key: str) -> None:
        _FAILED_LOGINS.pop(key, None)

    @classmethod
    async def login(
        cls,
        email: str,
        password: str,
        db: AsyncSession,
        client_identifier: Optional[str] = None,
    ) -> PortalLoginResponse:
        norm_email = email.strip().lower()
        key = f"{norm_email}:{client_identifier or 'default'}"
        cls._check_brute_force_lockout(key)

        user = await AgentPortalRepository.get_portal_user_by_email(norm_email, db)
        if not user:
            cls._record_failed_login(key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect portal credentials",
            )

        # Enforce role AI_AGENT_PORTAL_USER
        if user.role != "AI_AGENT_PORTAL_USER":
            logger.warning(f"[Portal Auth] User {user.email} attempted portal login with non-portal role {user.role}")
            cls._record_failed_login(key)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access restricted: This portal is reserved for AI Agent Portal accounts only.",
            )

        if not user.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your portal account has been deactivated. Please contact your organization administrator.",
            )

        if not verify_password(password, user.hashed_password):
            cls._record_failed_login(key)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect portal credentials",
            )

        # Successful login -> clear lockout tracker
        cls._clear_failed_logins(key)

        # Generate standard JWT access token
        tenant_id = user.business_id or "c3dce06c-8330-4724-ba33-b5282b3c3596"
        token = create_access_token(
            subject=user.id,
            role=user.role,
            tenant_id=tenant_id,
        )

        perms = user.permissions or {}
        access_scope = perms.get("access_scope", "ALL_AGENTS")
        assigned_agents = await AgentPortalRepository.get_user_assigned_agent_ids(user.id, db)

        user_info = PortalUserInfo(
            id=user.id,
            email=user.email,
            full_name=user.full_name or user.first_name or norm_email.split("@")[0],
            role=user.role,
            business_id=user.business_id,
            access_scope=access_scope,
            assigned_agent_ids=assigned_agents,
            is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else None,
        )

        logger.info(f"[Portal Auth] User {user.email} successfully logged into AI Agent Portal (scope={access_scope})")

        return PortalLoginResponse(
            access_token=token,
            business_id=user.business_id,
            tenant_id=tenant_id,
            role=user.role,
            user=user_info,
        )

    # -----------------------------------------------------------------------
    # Admin Portal User Management
    # -----------------------------------------------------------------------
    @classmethod
    async def create_portal_user(
        cls,
        payload: CreatePortalUserRequest,
        current_admin: User,
        db: AsyncSession,
    ) -> PortalUserInfo:
        norm_email = payload.email.strip().lower()
        existing = await AgentPortalRepository.get_portal_user_by_email(norm_email, db)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A user with this email address already exists.",
            )

        bid = current_admin.business_id or "c3dce06c-8330-4724-ba33-b5282b3c3596"
        scope = payload.access_scope if payload.access_scope in ("ALL_AGENTS", "SELECTED_AGENTS") else "ALL_AGENTS"
        agent_ids = payload.agent_ids or []

        new_user = User(
            email=norm_email,
            hashed_password=hash_password(payload.password),
            plain_password="",
            full_name=payload.name.strip(),
            role="AI_AGENT_PORTAL_USER",
            business_id=bid,
            is_active=True,
            email_verified=True,
            permissions={"access_scope": scope, "agent_ids": agent_ids},
        )
        db.add(new_user)
        await db.commit()
        await db.refresh(new_user)

        if scope == "SELECTED_AGENTS" and agent_ids:
            await AgentPortalRepository.set_user_agent_access(new_user.id, bid, agent_ids, db)

        return PortalUserInfo(
            id=new_user.id,
            email=new_user.email,
            full_name=new_user.full_name,
            role=new_user.role,
            business_id=new_user.business_id,
            access_scope=scope,
            assigned_agent_ids=agent_ids,
            is_active=new_user.is_active,
            created_at=new_user.created_at.isoformat() if new_user.created_at else None,
        )

    @classmethod
    async def list_portal_users(
        cls,
        current_admin: User,
        db: AsyncSession,
    ) -> List[Dict[str, Any]]:
        bid = current_admin.business_id or "c3dce06c-8330-4724-ba33-b5282b3c3596"
        return await AgentPortalRepository.list_portal_users(bid, db)

    @classmethod
    async def update_portal_user(
        cls,
        user_id: str,
        payload: UpdatePortalUserRequest,
        current_admin: User,
        db: AsyncSession,
    ) -> PortalUserInfo:
        user = await AgentPortalRepository.get_portal_user_by_id(user_id, db)
        if not user or user.role != "AI_AGENT_PORTAL_USER":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portal user not found.")

        # Multi-tenant check
        if current_admin.business_id and user.business_id != current_admin.business_id and current_admin.role != "SUPER_ADMIN":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden: User is in another tenant.")

        if payload.name is not None:
            user.full_name = payload.name.strip()
        if payload.password is not None and payload.password.strip():
            user.hashed_password = hash_password(payload.password.strip())
        if payload.is_active is not None:
            user.is_active = payload.is_active

        perms = dict(user.permissions or {})
        if payload.access_scope is not None:
            perms["access_scope"] = payload.access_scope
        if payload.agent_ids is not None:
            perms["agent_ids"] = payload.agent_ids
            bid = user.business_id or "default"
            await AgentPortalRepository.set_user_agent_access(user.id, bid, payload.agent_ids, db)

        user.permissions = perms
        db.add(user)
        await db.commit()
        await db.refresh(user)

        assigned_agents = await AgentPortalRepository.get_user_assigned_agent_ids(user.id, db)
        return PortalUserInfo(
            id=user.id,
            email=user.email,
            full_name=user.full_name,
            role=user.role,
            business_id=user.business_id,
            access_scope=perms.get("access_scope", "ALL_AGENTS"),
            assigned_agent_ids=assigned_agents,
            is_active=user.is_active,
            created_at=user.created_at.isoformat() if user.created_at else None,
        )

    # -----------------------------------------------------------------------
    # Agent Verification Helper
    # -----------------------------------------------------------------------
    @classmethod
    async def _verify_portal_access_to_agent(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
    ) -> Dict[str, Any]:
        agent_ctx = await AgentDashboardRepository.resolve_agent_entity(agent_id, db)
        if not agent_ctx:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Agent '{agent_id}' was not found.",
            )

        # If user is admin/super_admin, permit
        if (user.role or "").upper() in ("SUPER_ADMIN", "ADMIN", "BUSINESS_OWNER"):
            return agent_ctx

        # For portal users, check permission & tenant
        has_access = await AgentPortalRepository.check_portal_agent_access(user, agent_id, db)
        if not has_access:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden: You do not have permission to view this agent's reports.",
            )

        return agent_ctx

    # -----------------------------------------------------------------------
    # Portal Agents List
    # -----------------------------------------------------------------------
    @classmethod
    async def list_agents(
        cls,
        user: User,
        db: AsyncSession,
        search: Optional[str] = None,
        status_filter: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> PortalAgentsPageResponse:
        items, total = await AgentPortalRepository.list_accessible_agents(
            user=user,
            db=db,
            search=search,
            status_filter=status_filter,
            page=page,
            page_size=page_size,
        )
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalAgentsPageResponse(
            items=[PortalAgentItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
        )

    # -----------------------------------------------------------------------
    # Reused Agent Dashboard Endpoints
    # -----------------------------------------------------------------------
    @classmethod
    async def get_overview(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        filters: DashboardFiltersQuery,
    ) -> AgentDashboardSummary:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_summary(agent_id=agent_id, db=db, filters=filters, current_user=user)

    @classmethod
    async def get_calls(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        filters: DashboardFiltersQuery,
    ) -> CallsPageResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_calls(agent_id=agent_id, db=db, filters=filters, current_user=user)

    @classmethod
    async def get_call_detail(
        cls,
        agent_id: str,
        call_id: str,
        user: User,
        db: AsyncSession,
    ) -> CallDetailResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_call_detail(agent_id=agent_id, call_id=call_id, db=db, current_user=user)

    @classmethod
    async def get_history(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> HistoryPageResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_history(agent_id=agent_id, db=db, page=page, page_size=page_size, current_user=user)

    @classmethod
    async def get_queue(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        status_filter: Optional[str] = None,
        search: Optional[str] = None,
    ) -> QueuePageResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_queue(
            agent_id=agent_id,
            db=db,
            page=page,
            page_size=page_size,
            status_filter=status_filter,
            search=search,
            current_user=user,
        )

    @classmethod
    async def search_transcripts(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        q: str = "",
    ) -> TranscriptsSearchResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.search_transcripts(agent_id=agent_id, query_str=q, db=db, current_user=user)

    @classmethod
    async def get_filter_options(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
    ) -> FilterOptionsResponse:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.get_filter_options(agent_id=agent_id, db=db, current_user=user)

    @classmethod
    async def export_stream(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        export_type: str,
        tab: str,
        filters: DashboardFiltersQuery,
    ) -> bytes:
        await cls._verify_portal_access_to_agent(agent_id, user, db)
        return await AgentDashboardService.export_stream(
            agent_id=agent_id,
            db=db,
            export_type=export_type,
            tab=tab,
            filters=filters,
            current_user=user,
        )

    # -----------------------------------------------------------------------
    # End-to-End Extended Tabs (Recordings, Blacklist, Voice, Qualified, Completed)
    # -----------------------------------------------------------------------
    @classmethod
    async def get_recordings(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> PortalRecordingsResponse:
        agent_ctx = await cls._verify_portal_access_to_agent(agent_id, user, db)
        items, total = await AgentPortalRepository.get_recordings(agent_ctx, db, page=page, page_size=page_size)
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalRecordingsResponse(
            items=[PortalRecordingItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available=True,
        )

    @classmethod
    async def get_blacklist(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        search: Optional[str] = None,
    ) -> PortalBlacklistResponse:
        agent_ctx = await cls._verify_portal_access_to_agent(agent_id, user, db)
        items, total = await AgentPortalRepository.get_blacklist(agent_ctx, db, page=page, page_size=page_size, search=search)
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalBlacklistResponse(
            items=[PortalBlacklistItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available=True,
        )

    @classmethod
    async def get_voice(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> PortalVoiceResponse:
        agent_ctx = await cls._verify_portal_access_to_agent(agent_id, user, db)
        items, total = await AgentPortalRepository.get_voice_transfers(agent_ctx, db, page=page, page_size=page_size)
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalVoiceResponse(
            items=[PortalVoiceItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available=True,
            reason=None if total > 0 else "No voice agent calls or human handoff transfers recorded for this agent.",
        )

    @classmethod
    async def get_qualified(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> PortalQualifiedResponse:
        agent_ctx = await cls._verify_portal_access_to_agent(agent_id, user, db)
        items, total = await AgentPortalRepository.get_qualified_leads(agent_ctx, db, page=page, page_size=page_size)
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalQualifiedResponse(
            items=[PortalQualifiedLeadItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available=True,
        )

    @classmethod
    async def get_completed(
        cls,
        agent_id: str,
        user: User,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
    ) -> PortalCompletedResponse:
        agent_ctx = await cls._verify_portal_access_to_agent(agent_id, user, db)
        items, total = await AgentPortalRepository.get_completed_calls(agent_ctx, db, page=page, page_size=page_size)
        total_pages = math.ceil(total / page_size) if total > 0 else 0
        return PortalCompletedResponse(
            items=[PortalCompletedCallItem(**i) for i in items],
            page=page,
            page_size=page_size,
            total=total,
            total_pages=total_pages,
            available=True,
        )
