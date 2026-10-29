"""Module 16/17 — expert assignment + human handoff."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Expert, Lead, LeadAssignment


async def assign_least_loaded_expert(db: AsyncSession, business_id: str) -> Expert | None:
    """LEAST_LOADED assignment strategy (module 17)."""
    result = await db.scalars(
        select(Expert)
        .where(Expert.business_id == business_id, Expert.status != "OFFLINE")
        .order_by(Expert.active_lead_count.asc())
    )
    return result.first()


async def handoff_lead(db: AsyncSession, lead: Lead) -> Expert | None:
    expert = await assign_least_loaded_expert(db, lead.business_id)
    if not expert:
        return None

    lead.assigned_expert_id = expert.id
    lead.status = "ASSIGNED"
    expert.active_lead_count += 1

    db.add(LeadAssignment(lead_id=lead.id, expert_id=expert.id, strategy="LEAST_LOADED"))
    db.add(lead)
    db.add(expert)
    return expert


def build_handoff_summary(lead: Lead, score: int) -> str:
    """AI-generated handoff summary (module 16 example format)."""
    name = f"{lead.first_name} {lead.last_name}".strip() or "Unnamed lead"
    return (
        f"Lead: {name}\n"
        f"Requirement: {lead.requirement or 'Not captured'}\n"
        f"Budget: {lead.budget or 'Not captured'}\n"
        f"Timeline: {lead.timeline or 'Not captured'}\n"
        f"Qualification: {score}/100\n"
        f"Recommended Action: Immediate expert follow-up"
    )
