"""Module 5 — RBAC dependency helper.
Provides opt-in permission checks without breaking any existing endpoints.
"""
from typing import Callable
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User
from app.models.role import Role


def require_permission(permission_name: str) -> Callable:
    """Dependency factory checking whether current user holds the required permission.

    SUPER_ADMIN and ADMIN roles automatically pass all checks.
    """

    async def _dependency(
        user: User = Depends(get_current_db_user),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        user_role = (user.role or "").strip().upper()
        if user_role in ("SUPER_ADMIN", "ADMIN"):
            return user

        # 1. Check user.permissions JSON dict
        perms = user.permissions or {}
        if isinstance(perms, dict):
            if perms.get(permission_name) is True or perms.get("all") is True:
                return user
        elif isinstance(perms, list):
            if permission_name in perms or "*" in perms:
                return user

        # 2. Check relational Role and Permissions if available
        stmt = (
            select(Role)
            .where(Role.name.ilike(user_role))
        )
        res = await db.execute(stmt)
        roles = res.scalars().all()
        for r in roles:
            for p in r.permissions:
                if p.name == permission_name or p.name == "*":
                    return user

        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing required permission: '{permission_name}'",
        )

    return _dependency
