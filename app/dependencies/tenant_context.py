"""Module 2 — Tenant context dependency for multi-tenant isolation.
Purely additive: does not alter core security or existing auth routes.
"""
from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import PRIMARY_BUSINESS_ID, get_current_db_user
from app.core.security import get_current_user
from app.db.database import get_db
from app.db.models import User


async def get_current_tenant_id(
    token_payload: dict = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> str:
    """Resolves tenant_id strictly from JWT token or fallback to user database record."""
    tenant_id = token_payload.get("tenant_id") or token_payload.get("business_id")
    if tenant_id:
        return str(tenant_id)

    user = await db.get(User, token_payload.get("sub"))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authenticated user not found",
        )

    # Check tenant_id or business_id on user model
    user_tenant = getattr(user, "business_id", None) or getattr(user, "tenant_id", None)
    if user_tenant and str(user_tenant) not in ("None", "", "null"):
        return str(user_tenant)

    if (user.role or "").upper() == "SUPER_ADMIN":
        return PRIMARY_BUSINESS_ID

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="User is not assigned to a tenant workspace",
    )


async def get_tenant_user(
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
) -> User:
    """Returns the authenticated DB user ensuring tenant context is active."""
    return user
