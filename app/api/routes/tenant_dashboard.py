from datetime import datetime, timezone
from typing import Any, Dict, List
from fastapi import APIRouter, Depends
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import AIAgent, Call, Campaign, Lead, User
from app.dependencies.tenant_context import get_current_tenant_id

router = APIRouter(prefix="/api/tenant/dashboard", tags=["tenant-dashboard"])


@router.get("")
async def get_tenant_dashboard_kpis(
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_db_user),
) -> Dict[str, Any]:
    """Returns aggregated KPIs strictly scoped to the authenticated tenant."""

    # 1. Calls KPIs
    calls_count_stmt = select(func.count(Call.id)).where(Call.business_id == tenant_id)
    total_calls = (await db.execute(calls_count_stmt)).scalar() or 0

    completed_calls_stmt = select(func.count(Call.id)).where(
        Call.business_id == tenant_id,
        Call.status.ilike("completed"),
    )
    completed_calls = (await db.execute(completed_calls_stmt)).scalar() or 0

    duration_stmt = select(func.sum(Call.duration_seconds)).where(Call.business_id == tenant_id)
    total_duration = (await db.execute(duration_stmt)).scalar() or 0

    # 2. Leads KPIs
    total_leads_stmt = select(func.count(Lead.id)).where(Lead.business_id == tenant_id)
    total_leads = (await db.execute(total_leads_stmt)).scalar() or 0

    converted_leads_stmt = select(func.count(Lead.id)).where(
        Lead.business_id == tenant_id,
        Lead.status.in_(["QUALIFIED", "CONVERTED", "INTERESTED"]),
    )
    qualified_leads = (await db.execute(converted_leads_stmt)).scalar() or 0

    # 3. Campaigns KPIs
    total_campaigns_stmt = select(func.count(Campaign.id)).where(Campaign.business_id == tenant_id)
    total_campaigns = (await db.execute(total_campaigns_stmt)).scalar() or 0

    active_campaigns_stmt = select(func.count(Campaign.id)).where(
        Campaign.business_id == tenant_id,
        Campaign.status.ilike("running"),
    )
    active_campaigns = (await db.execute(active_campaigns_stmt)).scalar() or 0

    # 4. Agents KPIs
    total_agents_stmt = select(func.count(AIAgent.id)).where(AIAgent.business_id == tenant_id)
    total_agents = (await db.execute(total_agents_stmt)).scalar() or 0

    active_agents_stmt = select(func.count(AIAgent.id)).where(
        AIAgent.business_id == tenant_id,
        AIAgent.status.ilike("active"),
    )
    active_agents = (await db.execute(active_agents_stmt)).scalar() or 0

    # 5. Team Users Count
    total_users_stmt = select(func.count(User.id)).where(User.business_id == tenant_id)
    total_users = (await db.execute(total_users_stmt)).scalar() or 0

    # 6. Recent 5 Calls
    recent_calls_stmt = (
        select(Call)
        .where(Call.business_id == tenant_id)
        .order_by(desc(Call.initiated_at))
        .limit(5)
    )
    recent_calls_res = await db.execute(recent_calls_stmt)
    recent_calls_rows = recent_calls_res.scalars().all()

    recent_calls = [
        {
            "id": c.id,
            "direction": c.direction,
            "status": c.status,
            "duration_seconds": c.duration_seconds,
            "from_number": c.from_number,
            "to_number": c.to_number,
            "client_name": getattr(c, "client_name", ""),
            "initiated_at": c.initiated_at.isoformat() if c.initiated_at else None,
        }
        for c in recent_calls_rows
    ]

    return {
        "tenant_id": tenant_id,
        "kpis": {
            "calls": {
                "total": total_calls,
                "completed": completed_calls,
                "total_duration_seconds": int(total_duration),
                "avg_duration_seconds": round(total_duration / total_calls, 1) if total_calls > 0 else 0,
            },
            "leads": {
                "total": total_leads,
                "qualified": qualified_leads,
                "conversion_rate_pct": round((qualified_leads / total_leads) * 100, 1) if total_leads > 0 else 0.0,
            },
            "campaigns": {
                "total": total_campaigns,
                "active": active_campaigns,
            },
            "agents": {
                "total": total_agents,
                "active": active_agents,
            },
            "team_members": total_users,
        },
        "recent_calls": recent_calls,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
