from __future__ import annotations

from typing import List, Optional, Any
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.database import get_db
from app.db.models import User
from app.core.deps import get_current_db_user, get_optional_db_user
from app.schemas.agent_portal import (
    PortalLoginRequest,
    PortalLoginResponse,
    PortalUserInfo,
    CreatePortalUserRequest,
    UpdatePortalUserRequest,
)
from app.services.agent_portal_service import AgentPortalService
from app.repositories.agent_portal_repository import AgentPortalRepository

router = APIRouter(prefix="/api/v1/agent-portal", tags=["agent_portal_auth"])
api_router = APIRouter(prefix="/api/agent-portal", tags=["agent_portal_auth"])


# ---------------------------------------------------------------------------
# Strict Portal Dependencies (Guards)
# ---------------------------------------------------------------------------

async def get_current_portal_user(
    request: Request,
    user: Optional[User] = Depends(get_optional_db_user),
) -> User:
    """
    Enforces:
    1. Authenticated user with valid token.
    2. Role is AI_AGENT_PORTAL_USER (or admin for inspection).
    3. User account is active.
    4. Read-only guard: Portal users can only make GET / HEAD / OPTIONS requests.
    """
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please login with your AI Agent Portal account.",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Portal account is deactivated.",
        )

    # If portal user, enforce strict read-only on data endpoints
    if user.role == "AI_AGENT_PORTAL_USER":
        if request.method.upper() not in ("GET", "HEAD", "OPTIONS"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Portal accounts have read-only access. Write operations are strictly prohibited.",
            )
        return user

    # Allow admins to access portal endpoints as well
    if (user.role or "").upper() in ("SUPER_ADMIN", "ADMIN", "BUSINESS_OWNER"):
        return user

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Insufficient permissions to access AI Agent Portal.",
    )


async def require_portal_admin(
    user: User = Depends(get_current_db_user),
) -> User:
    """Enforces admin privileges for managing portal user logins."""
    role = (user.role or "").upper()
    if role not in ("SUPER_ADMIN", "ADMIN", "BUSINESS_OWNER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only administrators can manage AI Agent Portal users.",
        )
    return user


# ---------------------------------------------------------------------------
# A) Portal Authentication Endpoints
# ---------------------------------------------------------------------------

@router.post("/auth/login", response_model=PortalLoginResponse)
@api_router.post("/auth/login", response_model=PortalLoginResponse)
async def portal_login(
    payload: PortalLoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Dedicated authentication endpoint for AI Agent Portal users.
    Only accounts with role AI_AGENT_PORTAL_USER can authenticate.
    Protected with brute-force lockout safeguards.
    """
    client_ip = request.client.host if request.client else "unknown"
    return await AgentPortalService.login(
        email=payload.email,
        password=payload.password,
        db=db,
        client_identifier=client_ip,
    )


@router.get("/auth/me", response_model=PortalUserInfo)
@api_router.get("/auth/me", response_model=PortalUserInfo)
async def get_portal_me(
    current_user: User = Depends(get_current_portal_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves authenticated portal user's identity, tenant, and access scope."""
    perms = current_user.permissions or {}
    scope = perms.get("access_scope", "ALL_AGENTS")
    assigned = await AgentPortalRepository.get_user_assigned_agent_ids(current_user.id, db)

    return PortalUserInfo(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name or current_user.first_name or current_user.email.split("@")[0],
        role=current_user.role,
        business_id=current_user.business_id,
        access_scope=scope,
        assigned_agent_ids=assigned,
        is_active=current_user.is_active,
        created_at=current_user.created_at.isoformat() if current_user.created_at else None,
    )


@router.post("/auth/logout")
@api_router.post("/auth/logout")
async def portal_logout():
    """Stateless logout confirmation."""
    return {"status": "ok", "message": "Successfully logged out of AI Agent Portal"}


# ---------------------------------------------------------------------------
# B) Admin Portal User Management Endpoints
# ---------------------------------------------------------------------------

@router.post("/admin/users", response_model=PortalUserInfo, status_code=status.HTTP_201_CREATED)
@api_router.post("/admin/users", response_model=PortalUserInfo, status_code=status.HTTP_201_CREATED)
async def create_portal_user(
    payload: CreatePortalUserRequest,
    current_admin: User = Depends(require_portal_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint to create a new AI_AGENT_PORTAL_USER with access scope."""
    return await AgentPortalService.create_portal_user(
        payload=payload,
        current_admin=current_admin,
        db=db,
    )


@router.get("/admin/users", response_model=List[PortalUserInfo])
@api_router.get("/admin/users", response_model=List[PortalUserInfo])
async def list_portal_users(
    current_admin: User = Depends(require_portal_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint to list all AI_AGENT_PORTAL_USER logins in the organization."""
    users_data = await AgentPortalService.list_portal_users(
        current_admin=current_admin,
        db=db,
    )
    return [PortalUserInfo(**u) for u in users_data]


@router.patch("/admin/users/{user_id}", response_model=PortalUserInfo)
@api_router.patch("/admin/users/{user_id}", response_model=PortalUserInfo)
async def update_portal_user(
    user_id: str,
    payload: UpdatePortalUserRequest,
    current_admin: User = Depends(require_portal_admin),
    db: AsyncSession = Depends(get_db),
):
    """Admin endpoint to activate/deactivate, reset password, or change agent access scope."""
    return await AgentPortalService.update_portal_user(
        user_id=user_id,
        payload=payload,
        current_admin=current_admin,
        db=db,
    )
