from fastapi import APIRouter
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.db.database import get_db
from app.db.models import Lead
from app.schemas.lead import LeadOut, WebhookLeadCreate

router = APIRouter(prefix="/api/webhooks/leads", tags=["webhooks"])


@router.post("/{source}", response_model=LeadOut, status_code=201)
async def capture_lead(source: str, payload: WebhookLeadCreate, db: AsyncSession = Depends(get_db)) -> Lead:
    """Module 10 — normalizes leads from any external source into the Lead model.
    No auth: this is a public ingestion endpoint (secure with a per-business
    webhook signing secret in production — see module 37)."""
    lead = Lead(
        business_id=payload.business_id,
        source=source.upper(),
        first_name=payload.first_name,
        last_name=payload.last_name,
        email=payload.email,
        phone=payload.phone,
        company=payload.company,
        requirement=payload.requirement,
        agent_id=payload.agent_id,
    )
    db.add(lead)
    await db.commit()
    await db.refresh(lead)
    return lead
