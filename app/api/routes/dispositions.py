import uuid
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, desc, asc

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import User, CallDisposition, Business
from app.api.routes.websockets import manager

router = APIRouter(tags=["Call Dispositions & System Statuses"])


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


DEFAULT_DISPOSITIONS = [
    {
        "status_code": "SALE",
        "status_name": "Sale / Success",
        "description": "Customer agreed to purchase or enrolled successfully.",
        "category": "POSITIVE",
        "color": "emerald",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 1,
    },
    {
        "status_code": "CALLBK",
        "status_name": "Callback Required",
        "description": "Customer requested a scheduled callback at a specific time.",
        "category": "CALLBACK",
        "color": "indigo",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 2,
    },
    {
        "status_code": "INTEREST",
        "status_name": "Interested / Follow Up",
        "description": "Lead showed positive intent, requires nurture follow up.",
        "category": "POSITIVE",
        "color": "emerald",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 3,
    },
    {
        "status_code": "CBUSY",
        "status_name": "Customer Busy / Call Later",
        "description": "Prospect is currently occupied, asked to try again shortly.",
        "category": "CALLBACK",
        "color": "amber",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 4,
    },
    {
        "status_code": "NI",
        "status_name": "Not Interested",
        "description": "Customer explicitly declined offer or service.",
        "category": "NEGATIVE",
        "color": "rose",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 5,
    },
    {
        "status_code": "VM",
        "status_name": "Voicemail / No Answer",
        "description": "Call routed to answering machine or rang out with no answer.",
        "category": "SYSTEM",
        "color": "amber",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 6,
    },
    {
        "status_code": "INV",
        "status_name": "Invalid Number",
        "description": "Phone number is disconnected, out of service, or incorrect.",
        "category": "SYSTEM",
        "color": "slate",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 7,
    },
    {
        "status_code": "DNC",
        "status_name": "Do Not Call (DNC)",
        "description": "Customer requested to be placed on the internal Do Not Call list.",
        "category": "NEGATIVE",
        "color": "rose",
        "is_selectable": True,
        "is_active": True,
        "is_system": True,
        "order_index": 8,
    },
]


async def ensure_default_dispositions(db: AsyncSession, business_id: Optional[str] = None):
    """Ensure standard system dispositions exist in the database."""
    query = select(CallDisposition)
    if business_id:
        query = query.where(or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None)))
    
    existing = (await db.execute(query)).scalars().all()
    if not existing:
        for item in DEFAULT_DISPOSITIONS:
            disp = CallDisposition(
                id=_uuid(),
                business_id=business_id,
                status_code=item["status_code"],
                status_name=item["status_name"],
                description=item["description"],
                category=item["category"],
                color=item["color"],
                is_selectable=item["is_selectable"],
                is_active=item["is_active"],
                is_system=item["is_system"],
                order_index=item["order_index"],
                created_at=_now(),
                updated_at=_now(),
            )
            db.add(disp)
        await db.commit()


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class DispositionCreateRequest(BaseModel):
    status_name: str = Field(..., min_length=1, max_length=120)
    status_code: Optional[str] = Field(None, max_length=30)
    description: Optional[str] = Field("", max_length=255)
    category: Optional[str] = Field("GENERAL", max_length=50)  # POSITIVE, NEGATIVE, CALLBACK, SYSTEM, GENERAL
    color: Optional[str] = Field("indigo", max_length=30)
    is_selectable: Optional[bool] = True
    is_active: Optional[bool] = True


class DispositionUpdateRequest(BaseModel):
    status_name: Optional[str] = Field(None, min_length=1, max_length=120)
    status_code: Optional[str] = Field(None, max_length=30)
    description: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=50)
    color: Optional[str] = Field(None, max_length=30)
    is_selectable: Optional[bool] = None
    is_active: Optional[bool] = None
    order_index: Optional[int] = None


# ---------------------------------------------------------------------------
# Public / Agent Endpoint: Active Dispositions List
# ---------------------------------------------------------------------------

@router.get("/api/dispositions")
async def get_active_dispositions(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieve active and selectable dispositions for Voice Agent Dashboard & Supervisor screen."""
    business_id = user.business_id
    await ensure_default_dispositions(db, business_id)

    query = select(CallDisposition).where(
        CallDisposition.is_active == True,
        CallDisposition.is_selectable == True
    )
    if business_id:
        query = query.where(or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None)))
    
    query = query.order_by(asc(CallDisposition.order_index), asc(CallDisposition.status_name))
    items = (await db.execute(query)).scalars().all()

    # Return array of string options for backwards compatibility with callService.getDispositions()
    options = [item.status_name for item in items]
    
    # Also return detailed list format
    detailed = [
        {
            "id": item.id,
            "code": item.status_code,
            "name": item.status_name,
            "description": item.description,
            "category": item.category,
            "color": item.color,
            "is_system": item.is_system,
        }
        for item in items
    ]

    return {
        "options": options,
        "items": detailed,
    }


# ---------------------------------------------------------------------------
# Admin Endpoints: Full Management CRUD
# ---------------------------------------------------------------------------

@router.get("/api/admin/dispositions")
async def get_admin_dispositions(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    category: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Admin endpoint to view and manage all dispositions/system statuses."""
    business_id = user.business_id
    await ensure_default_dispositions(db, business_id)

    query = select(CallDisposition)
    if business_id:
        query = query.where(or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None)))

    if category and category != "ALL":
        query = query.where(CallDisposition.category == category)

    if search:
        search_pattern = f"%{search}%"
        query = query.where(
            or_(
                CallDisposition.status_name.ilike(search_pattern),
                CallDisposition.status_code.ilike(search_pattern),
                CallDisposition.description.ilike(search_pattern)
            )
        )

    query = query.order_by(asc(CallDisposition.order_index), asc(CallDisposition.status_name))
    items = (await db.execute(query)).scalars().all()

    return [
        {
            "id": item.id,
            "code": item.status_code,
            "name": item.status_name,
            "description": item.description or "",
            "category": item.category,
            "color": item.color or "indigo",
            "is_selectable": item.is_selectable,
            "is_active": item.is_active,
            "is_system": item.is_system,
            "order_index": item.order_index,
            "created_at": item.created_at.isoformat() if item.created_at else "",
            "updated_at": item.updated_at.isoformat() if item.updated_at else "",
        }
        for item in items
    ]


@router.post("/api/admin/dispositions", status_code=status.HTTP_201_CREATED)
async def create_disposition(
    payload: DispositionCreateRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin creates a new disposition / system status."""
    business_id = user.business_id
    
    # Auto-generate code if empty
    code = (payload.status_code or "").strip().upper()
    if not code:
        code = "".join(c for c in payload.status_name[:6].upper() if c.isalnum())
        if not code:
            code = "DISPO"

    # Check for duplicate code in same business
    dup_q = select(CallDisposition).where(
        CallDisposition.status_code == code,
        or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None))
    )
    dup = (await db.execute(dup_q)).scalars().first()
    if dup:
        code = f"{code[:5]}_{uuid.uuid4().hex[:3].upper()}"

    max_order = (await db.scalar(
        select(CallDisposition.order_index)
        .where(or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None)))
        .order_by(desc(CallDisposition.order_index))
    )) or 0

    new_disp = CallDisposition(
        id=_uuid(),
        business_id=business_id,
        status_code=code,
        status_name=payload.status_name.strip(),
        description=(payload.description or "").strip(),
        category=(payload.category or "GENERAL").upper(),
        color=payload.color or "indigo",
        is_selectable=payload.is_selectable if payload.is_selectable is not None else True,
        is_active=payload.is_active if payload.is_active is not None else True,
        is_system=False,
        order_index=max_order + 1,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(new_disp)
    await db.commit()
    await db.refresh(new_disp)

    # Real-time WebSocket broadcast to all connected agents and supervisors
    try:
        await manager.broadcast({
            "type": "DISPOSITIONS_UPDATED",
            "business_id": business_id,
            "action": "CREATE",
            "disposition_id": new_disp.id,
            "disposition_name": new_disp.status_name,
            "timestamp": _now().isoformat(),
        })
    except Exception:
        pass

    return {
        "success": True,
        "message": f"Disposition '{new_disp.status_name}' created successfully.",
        "item": {
            "id": new_disp.id,
            "code": new_disp.status_code,
            "name": new_disp.status_name,
            "description": new_disp.description,
            "category": new_disp.category,
            "color": new_disp.color,
            "is_selectable": new_disp.is_selectable,
            "is_active": new_disp.is_active,
            "is_system": new_disp.is_system,
        }
    }


@router.put("/api/admin/dispositions/{disp_id}")
async def update_disposition(
    disp_id: str,
    payload: DispositionUpdateRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin updates an existing disposition."""
    business_id = user.business_id
    disp = await db.get(CallDisposition, disp_id)
    if not disp:
        raise HTTPException(status_code=404, detail="Disposition not found")

    if payload.status_name is not None:
        disp.status_name = payload.status_name.strip()
    if payload.status_code is not None:
        disp.status_code = payload.status_code.strip().upper()
    if payload.description is not None:
        disp.description = payload.description.strip()
    if payload.category is not None:
        disp.category = payload.category.upper()
    if payload.color is not None:
        disp.color = payload.color
    if payload.is_selectable is not None:
        disp.is_selectable = payload.is_selectable
    if payload.is_active is not None:
        disp.is_active = payload.is_active
    if payload.order_index is not None:
        disp.order_index = payload.order_index

    disp.updated_at = _now()
    await db.commit()
    await db.refresh(disp)

    # Real-time WebSocket broadcast to all connected agents and supervisors
    try:
        await manager.broadcast({
            "type": "DISPOSITIONS_UPDATED",
            "business_id": business_id,
            "action": "UPDATE",
            "disposition_id": disp.id,
            "disposition_name": disp.status_name,
            "timestamp": _now().isoformat(),
        })
    except Exception:
        pass

    return {
        "success": True,
        "message": f"Disposition '{disp.status_name}' updated.",
        "item": {
            "id": disp.id,
            "code": disp.status_code,
            "name": disp.status_name,
            "description": disp.description,
            "category": disp.category,
            "color": disp.color,
            "is_selectable": disp.is_selectable,
            "is_active": disp.is_active,
        }
    }


@router.delete("/api/admin/dispositions/{disp_id}")
async def delete_disposition(
    disp_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Admin deletes or disables a disposition."""
    business_id = user.business_id
    disp = await db.get(CallDisposition, disp_id)
    if not disp:
        raise HTTPException(status_code=404, detail="Disposition not found")

    name = disp.status_name
    await db.delete(disp)
    await db.commit()

    # Real-time WebSocket broadcast to all connected agents and supervisors
    try:
        await manager.broadcast({
            "type": "DISPOSITIONS_UPDATED",
            "business_id": business_id,
            "action": "DELETE",
            "disposition_id": disp_id,
            "timestamp": _now().isoformat(),
        })
    except Exception:
        pass

    return {"success": True, "message": f"Disposition '{name}' deleted successfully."}


@router.post("/api/admin/dispositions/reset-defaults")
async def reset_default_dispositions(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Reset dispositions to standard defaults."""
    business_id = user.business_id
    
    # Delete existing
    query = select(CallDisposition)
    if business_id:
        query = query.where(or_(CallDisposition.business_id == business_id, CallDisposition.business_id.is_(None)))
    existing = (await db.execute(query)).scalars().all()
    for item in existing:
        await db.delete(item)
    await db.flush()

    # Re-create defaults
    for item in DEFAULT_DISPOSITIONS:
        disp = CallDisposition(
            id=_uuid(),
            business_id=business_id,
            status_code=item["status_code"],
            status_name=item["status_name"],
            description=item["description"],
            category=item["category"],
            color=item["color"],
            is_selectable=item["is_selectable"],
            is_active=item["is_active"],
            is_system=item["is_system"],
            order_index=item["order_index"],
            created_at=_now(),
            updated_at=_now(),
        )
        db.add(disp)

    await db.commit()

    try:
        await manager.broadcast({
            "type": "DISPOSITIONS_UPDATED",
            "business_id": business_id,
            "action": "RESET_DEFAULTS",
            "timestamp": _now().isoformat(),
        })
    except Exception:
        pass

    return {"success": True, "message": "Dispositions reset to factory defaults."}
