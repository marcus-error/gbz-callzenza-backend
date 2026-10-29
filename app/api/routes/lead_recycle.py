from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, desc
from loguru import logger

from app.core.deps import get_current_business_id, get_current_db_user, get_current_tenant_id
from app.db.database import get_db
from app.db.models import User, Business, RecycleList
from app.services import lead_recycle_service

router = APIRouter(prefix="/api/leads", tags=["Lead Recycle & Hopper"])


class CreateRecycleListRequest(BaseModel):
    source_disposition: str = "Call Back"
    source_campaign: str = "Product Campaign"
    lead_selection: Optional[str] = "All eligible leads"
    retry_window: Optional[str] = "Next available window"
    default_hopper_queue: Optional[str] = "Auto Route"
    list_name: Optional[str] = None


class RecycleLeadsRequest(BaseModel):
    leadIds: List[str]
    campaign: str
    lane: Optional[str] = "Auto Route to Hopper"


@router.get("/recycle/kpis")
async def get_recycle_kpis(
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve real-time KPIs for Lead Recycle overview."""
    try:
        business_id = user.business_id or tenant_id
        return await lead_recycle_service.get_recycle_kpis(db=db, business_id=business_id)
    except Exception as exc:
        logger.error(f"Error in get_recycle_kpis: {exc}")
        return [
            {"label": "Recycle Lists", "value": 0, "cls": ""},
            {"label": "Eligible Leads", "value": 0, "cls": ""},
            {"label": "Call Back", "value": 0, "cls": "purple"},
            {"label": "Voice Mail", "value": 0, "cls": "orange"},
            {"label": "In Hopper", "value": 0, "cls": "green"},
        ]


@router.get("/recycle/lists")
async def get_recycle_lists(
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    query: str = Query(default=""),
    disposition: str = Query(default=""),
    campaign: str = Query(default=""),
    status: str = Query(default=""),
):
    """Retrieve filtered recycle lead lists."""
    business_id = user.business_id or tenant_id
    try:
        return await lead_recycle_service.get_recycle_lists(
            db=db,
            business_id=business_id,
            query=query,
            disposition=disposition,
            campaign=campaign,
            status=status,
        )
    except Exception as exc:
        logger.error(f"Error in get_recycle_lists: {exc}")
        await db.rollback()
        try:
            r_lists = (await db.execute(
                select(RecycleList)
                .where(RecycleList.business_id == business_id)
                .order_by(desc(RecycleList.created_at))
            )).scalars().all()
            return [
                {
                    "id": rl.id,
                    "icon": rl.icon,
                    "status": rl.status,
                    "statusBadge": rl.status_badge,
                    "title": rl.title,
                    "description": rl.description,
                    "tags": rl.tags or [],
                    "campaign": rl.source_campaign_name or "All Campaigns",
                    "total": rl.total_count or 0,
                    "pending": rl.pending_count or 0,
                    "recycled": rl.recycled_count or 0,
                    "created": rl.created_at.strftime("%I:%M %p") if rl.created_at else "",
                    "reason": rl.source_disposition,
                    "search": rl.title.lower(),
                    "actionLabel": "Open Lead List →",
                    "actionStyle": "primary"
                }
                for rl in r_lists
            ]
        except Exception:
            return []


@router.get("/recycle/lists/{list_id}/leads")
async def get_leads_for_recycle_list(
    list_id: str,
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, le=1000),
):
    """Retrieve leads associated with a specific recycle list."""
    business_id = user.business_id or tenant_id
    return await lead_recycle_service.get_leads_for_recycle_list(
        db=db,
        business_id=business_id,
        list_id=list_id,
        limit=limit,
    )


@router.get("/recycle/lists/{list_id}/export")
async def export_recycle_list_csv(
    list_id: str,
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Export leads in a recycle list to a downloadable CSV file."""
    from fastapi.responses import Response
    business_id = user.business_id or tenant_id
    csv_content = await lead_recycle_service.export_recycle_list_csv(
        db=db,
        business_id=business_id,
        list_id=list_id,
    )
    return Response(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="recycled_leads_{list_id}.csv"'}
    )


@router.post("/recycle/lists")
async def create_recycle_list(
    payload: CreateRecycleListRequest,
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Create a new recycle lead list from disposition."""
    business_id = user.business_id or tenant_id
    return await lead_recycle_service.create_recycle_list(
        db=db,
        business_id=business_id,
        payload=payload.model_dump(),
    )


@router.post("/recycle/execute")
async def execute_recycle_leads(
    payload: RecycleLeadsRequest,
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Recycle selected leads into the Lead Hopper."""
    business_id = user.business_id or tenant_id
    return await lead_recycle_service.recycle_leads(
        db=db,
        business_id=business_id,
        lead_ids=payload.leadIds,
        campaign_name=payload.campaign,
        lane=payload.lane or "Auto Route to Hopper",
        user_id=user.id,
    )


@router.get("/hopper/kpis")
async def get_hopper_kpis(
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve real-time KPIs for Lead Hopper calling queue."""
    business_id = user.business_id or tenant_id
    return await lead_recycle_service.get_hopper_kpis(db=db, business_id=business_id)


@router.get("/hopper/leads")
async def get_hopper_leads(
    user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
    queue: str = Query(default=""),
    campaign: str = Query(default=""),
    status: str = Query(default=""),
    search: str = Query(default=""),
):
    """Retrieve active hopper calling leads."""
    business_id = user.business_id or tenant_id
    return await lead_recycle_service.get_hopper_leads(
        db=db,
        business_id=business_id,
        queue=queue,
        campaign=campaign,
        status=status,
        search=search,
    )
