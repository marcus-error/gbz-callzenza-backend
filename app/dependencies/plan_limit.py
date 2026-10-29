"""Module 8 — Plan limit enforcement dependency.
Optional dependency that endpoints can use to ensure tenant does not exceed subscription tiers.
"""
from typing import Callable
from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.plan import Plan, Subscription


def check_plan_limit(feature_name: str) -> Callable:
    """Dependency factory checking if tenant is authorized to use this feature under current plan."""

    async def _dependency(
        tenant_id: str = Depends(get_current_tenant_id),
        db: AsyncSession = Depends(get_db),
    ) -> Subscription:
        stmt = (
            select(Subscription)
            .where(
                Subscription.tenant_id == tenant_id,
                Subscription.status == "ACTIVE",
            )
            .order_by(Subscription.created_at.desc())
        )
        res = await db.execute(stmt)
        sub = res.scalar_one_or_none()

        # If no explicit subscription found, allow baseline free usage
        if not sub or not sub.plan:
            return sub

        features = sub.plan.features or {}
        # If feature specifically turned off
        if features.get(feature_name) is False:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail=f"Feature '{feature_name}' is not enabled for your current subscription plan.",
            )

        return sub

    return _dependency
