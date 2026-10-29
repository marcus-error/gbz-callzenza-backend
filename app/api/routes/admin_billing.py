from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Optional

from app.core.deps import require_admin
from app.db.database import get_db
from app.db.models import User, Call, Lead, Campaign, Message

router = APIRouter(prefix="/api/admin/billing", tags=["admin_billing"])


class UsageOut(BaseModel):
    total_minutes: float
    sms_count: int
    estimated_cost_usd: float
    total_calls: int


class AnalyticsOut(BaseModel):
    campaign_success_rate: float
    qualified_lead_rate: float
    total_campaigns: int
    total_leads: int


@router.get("/usage", response_model=UsageOut)
async def get_usage_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    # Sum call duration
    duration_stmt = select(func.sum(Call.duration_seconds)).where(Call.status == "COMPLETED")
    total_seconds = await db.scalar(duration_stmt) or 0
    total_minutes = round(total_seconds / 60.0, 1)

    calls_count_stmt = select(func.count(Call.id))
    total_calls = await db.scalar(calls_count_stmt) or 0

    # Real message/SMS count from database
    sms_count = await db.scalar(select(func.count(Message.id))) or 0

    # Cost estimate: $0.12 per call minute + $0.04 per SMS
    estimated_cost = round((total_minutes * 0.12) + (sms_count * 0.04), 2)

    return {
        "total_minutes": total_minutes,
        "sms_count": sms_count,
        "estimated_cost_usd": estimated_cost,
        "total_calls": total_calls,
    }


@router.get("/analytics", response_model=AnalyticsOut)
async def get_analytics_summary(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
) -> dict:
    total_campaigns = await db.scalar(select(func.count(Campaign.id))) or 0
    total_leads = await db.scalar(select(func.count(Lead.id))) or 0

    # Campaign success rate: completed / total campaigns
    completed_campaigns = await db.scalar(select(func.count(Campaign.id)).where(Campaign.status == "COMPLETED")) or 0
    campaign_success_rate = round((completed_campaigns / total_campaigns * 100) if total_campaigns > 0 else 0.0, 1)

    # Qualified lead rate: qualified / total leads
    qualified_leads = await db.scalar(select(func.count(Lead.id)).where(Lead.status == "QUALIFIED")) or 0
    qualified_lead_rate = round((qualified_leads / total_leads * 100) if total_leads > 0 else 0.0, 1)

    return {
        "campaign_success_rate": campaign_success_rate,
        "qualified_lead_rate": qualified_lead_rate,
        "total_campaigns": total_campaigns,
        "total_leads": total_leads,
    }
