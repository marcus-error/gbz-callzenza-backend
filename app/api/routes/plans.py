from datetime import datetime, timedelta, timezone
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, require_admin
from app.db.database import get_db
from app.db.models import User
from app.dependencies.tenant_context import get_current_tenant_id
from app.models.plan import Plan, Subscription
from app.schemas.plan import PlanCreate, PlanOut, SubscriptionCreate, SubscriptionOut

router = APIRouter(prefix="/api", tags=["plans-and-subscriptions"])


@router.get("/plans", response_model=List[PlanOut])
async def list_plans(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> List[PlanOut]:
    """List all available public plans."""
    stmt = select(Plan).where(Plan.is_active == True).order_by(Plan.price_cents.asc())
    res = await db.execute(stmt)
    return res.scalars().all()


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: PlanCreate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> PlanOut:
    """Create a new plan (Admin only)."""
    slug_clean = payload.slug.strip().lower()
    existing = await db.execute(select(Plan).where(Plan.slug == slug_clean))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Plan with this slug already exists")

    plan = Plan(
        name=payload.name.strip(),
        slug=slug_clean,
        description=payload.description.strip(),
        price_cents=payload.price_cents,
        billing_interval=payload.billing_interval,
        max_agents=payload.max_agents,
        max_concurrent_calls=payload.max_concurrent_calls,
        max_leads=payload.max_leads,
        max_minutes_monthly=payload.max_minutes_monthly,
        features=payload.features or {},
        is_active=True,
    )
    db.add(plan)
    await db.commit()
    await db.refresh(plan)
    return plan


@router.get("/subscriptions/current", response_model=SubscriptionOut)
async def get_current_subscription(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> SubscriptionOut:
    """Retrieve current subscription for the authenticated tenant."""
    stmt = (
        select(Subscription)
        .where(Subscription.tenant_id == tenant_id, Subscription.status == "ACTIVE")
        .order_by(Subscription.created_at.desc())
    )
    res = await db.execute(stmt)
    sub = res.scalar_one_or_none()

    if not sub:
        # Check if default plan exists or create a free subscription
        plan_stmt = select(Plan).where(Plan.slug == "free")
        free_plan = (await db.execute(plan_stmt)).scalar_one_or_none()
        if not free_plan:
            free_plan = Plan(
                name="Starter Free",
                slug="free",
                description="Default free plan",
                price_cents=0,
                billing_interval="monthly",
                max_agents=2,
                max_concurrent_calls=1,
                max_leads=200,
                max_minutes_monthly=60,
                features={"barge": False, "transfers": True, "api_access": True},
            )
            db.add(free_plan)
            await db.commit()
            await db.refresh(free_plan)

        sub = Subscription(
            tenant_id=tenant_id,
            plan_id=free_plan.id,
            status="ACTIVE",
            auto_renew=True,
            start_date=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(sub)
        await db.commit()
        await db.refresh(sub)

    return sub


@router.post("/subscriptions", response_model=SubscriptionOut, status_code=status.HTTP_201_CREATED)
async def subscribe_to_plan(
    payload: SubscriptionCreate,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> SubscriptionOut:
    """Upgrade, downgrade, or initiate a plan subscription for the current tenant."""
    plan = await db.get(Plan, payload.plan_id)
    if not plan:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")

    # Deactivate existing active subscriptions
    stmt = select(Subscription).where(
        Subscription.tenant_id == tenant_id,
        Subscription.status == "ACTIVE",
    )
    existing_subs = (await db.execute(stmt)).scalars().all()
    for s in existing_subs:
        s.status = "UPGRADED"
        db.add(s)

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    days = 365 if plan.billing_interval == "yearly" else 30
    end_date = now + timedelta(days=days)

    new_sub = Subscription(
        tenant_id=tenant_id,
        plan_id=plan.id,
        status="ACTIVE",
        auto_renew=payload.auto_renew,
        start_date=now,
        end_date=end_date,
        payment_method_id=payload.payment_method_id,
    )
    db.add(new_sub)
    await db.commit()
    await db.refresh(new_sub)
    return new_sub
