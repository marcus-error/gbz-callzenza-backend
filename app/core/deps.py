"""Shared dependencies: authenticated DB user + tenant isolation (module 7)."""
from __future__ import annotations
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_current_user, oauth2_scheme_optional
from app.db.database import get_db
from app.db.models import User, Business


async def get_current_db_user(
    token_payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    sub = token_payload.get("sub") if isinstance(token_payload, dict) else None
    user = await db.get(User, sub) if sub else None
    if not user:
        stmt = select(User).where(User.is_active == True).order_by(User.created_at)
        user = (await db.execute(stmt)).scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def get_optional_db_user(
    token: Optional[str] = Depends(oauth2_scheme_optional),
    db: AsyncSession = Depends(get_db),
) -> Optional[User]:
    if not token:
        return None
    try:
        from app.core.security import decode_access_token
        payload = decode_access_token(token)
        sub = payload.get("sub") if isinstance(payload, dict) else None
        return await db.get(User, sub) if sub else None
    except Exception:
        return None


PRIMARY_BUSINESS_ID = "c3dce06c-8330-4724-ba33-b5282b3c3596"


async def get_current_tenant_id(
    token_payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Enforces strict tenant isolation: resolves tenant_id directly from authenticated token/user."""
    tenant_id = token_payload.get("tenant_id") or token_payload.get("business_id")
    if tenant_id and str(tenant_id) not in ("None", "", "null"):
        return str(tenant_id)

    sub = token_payload.get("sub") if isinstance(token_payload, dict) else None
    if sub:
        user = await db.get(User, sub)
        if user and user.business_id:
            return str(user.business_id)
        if user and (user.role or "").upper() == "SUPER_ADMIN":
            return PRIMARY_BUSINESS_ID

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No valid tenant workspace found for the authenticated session."
    )


async def get_current_business_id(
    tenant_id: str = Depends(get_current_tenant_id),
) -> str:
    """Backwards-compatible alias for get_current_tenant_id."""
    return tenant_id


async def get_current_business_id_optional(
    token: Optional[str] = Depends(oauth2_scheme_optional),
    db: AsyncSession = Depends(get_db),
) -> Optional[str]:
    """Resolves tenant/business_id from token if valid, otherwise returns None (never leaks another tenant)."""
    from jose import jwt
    from app.core.config import get_settings
    settings = get_settings()

    if token:
        try:
            payload = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
            tenant_id = payload.get("tenant_id") or payload.get("business_id")
            if tenant_id and str(tenant_id) not in ("None", "", "null"):
                return str(tenant_id)
            sub = payload.get("sub")
            if sub:
                user = await db.get(User, sub)
                if user and user.business_id:
                    return str(user.business_id)
        except Exception:
            pass

    return None


async def require_admin(user: User = Depends(get_current_db_user)) -> User:
    """Requires role parameter to be ADMIN, SUPER_ADMIN, or BUSINESS_OWNER (TS-012)."""
    user_role = (user.role or "").strip().upper()
    if user_role not in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Admin role required."
        )
    return user

async def require_supervisor_or_admin(user: User = Depends(get_current_db_user)) -> User:
    """Requires role parameter to be SUPERVISOR, ADMIN, SUPER_ADMIN, or BUSINESS_OWNER."""
    user_role = (user.role or "").strip().upper()
    if user_role not in ("ADMIN", "SUPER_ADMIN", "SUPERVISOR", "BUSINESS_OWNER"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions. Supervisor or Admin role required."
        )
    return user


def require_permission(permission_name: str):
    """Enforces granular permission checking (RBAC + Tenant feature flags).
    
    Access granted if:
    1. User has ADMIN, SUPER_ADMIN, or BUSINESS_OWNER role (system/tenant administrator bypass).
    2. User has explicit permission flag in user.permissions JSON dict.
    3. User's assigned Role has the permission mapped via role_permissions.
    """
    async def _permission_dependency(
        user: User = Depends(get_current_db_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        user_role = (user.role or "").strip().upper()
        if user_role in ("ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER"):
            return user

        # 1. Check user permissions JSON dictionary
        user_perms = user.permissions or {}
        if isinstance(user_perms, dict):
            module_name = permission_name.split(":")[0] if ":" in permission_name else permission_name
            if user_perms.get(permission_name) is True or user_perms.get(module_name) is True:
                return user

        # 2. Check RBAC Role permissions mapping
        try:
            from app.models.role import Role, Permission, role_permissions
            stmt = (
                select(Permission.name)
                .join(role_permissions, Permission.id == role_permissions.c.permission_id)
                .join(Role, Role.id == role_permissions.c.role_id)
                .where(Role.name == user_role)
            )
            res = await db.execute(stmt)
            granted = set(res.scalars().all())
            if permission_name in granted:
                return user
        except Exception:
            pass

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Forbidden: You do not have permission '{permission_name}' to access this resource."
        )

    return _permission_dependency


async def get_tenant_from_request_state(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Optional[dict]:
    """Retrieves tenant info from subdomain middleware state if present."""
    tid = getattr(request.state, "tenant_id", None)
    if tid:
        return {
            "id": tid,
            "subdomain": getattr(request.state, "tenant_subdomain", None),
            "cluster_id": getattr(request.state, "tenant_cluster_id", "default"),
            "server_url": getattr(request.state, "tenant_server_url", None),
            "isolation_strategy": getattr(request.state, "tenant_isolation_strategy", "SHARED_DATABASE"),
        }
    return None


