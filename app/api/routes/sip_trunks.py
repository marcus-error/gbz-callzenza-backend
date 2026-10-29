"""
SIP Trunks Management API Routes
Provides CRUD and diagnostic testing for SIP trunks associated with carriers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, get_current_tenant_id, require_admin
from app.db.database import get_db
from app.db.models import SIPCarrier, SIPTrunk, DIDNumber, User
from app.services.sip_service import sip_service

router = APIRouter(prefix="/api/sip-trunks", tags=["sip_trunks"])


class TrunkCreatePayload(BaseModel):
    carrier_id: str
    name: str
    direction: str = "BIDIRECTIONAL"  # INBOUND, OUTBOUND, BIDIRECTIONAL
    max_concurrency: int = 10
    priority: int = 1
    proxy_server: Optional[str] = None
    register_enabled: bool = False
    status: str = "ACTIVE"  # ACTIVE, DISABLED


class TrunkUpdatePayload(BaseModel):
    name: Optional[str] = None
    direction: Optional[str] = None
    max_concurrency: Optional[int] = None
    priority: Optional[int] = None
    proxy_server: Optional[str] = None
    register_enabled: Optional[bool] = None
    registration_status: Optional[str] = None
    status: Optional[str] = None


def _trunk_to_out(t: SIPTrunk, carrier: Optional[SIPCarrier] = None, did_count: int = 0) -> Dict[str, Any]:
    return {
        "id": t.id,
        "carrier_id": t.carrier_id,
        "carrier_name": carrier.name if carrier else None,
        "carrier_host": carrier.sip_host if carrier else None,
        "carrier_port": carrier.sip_port if carrier else 5060,
        "carrier_status": carrier.status if carrier else None,
        "carrier_last_health": carrier.last_health_status if carrier else "UNKNOWN",
        "business_id": t.business_id,
        "name": t.name,
        "direction": t.direction,
        "max_concurrency": t.max_concurrency,
        "priority": t.priority,
        "proxy_server": t.proxy_server,
        "register_enabled": t.register_enabled,
        "registration_status": t.registration_status,
        "status": t.status,
        "assigned_did_count": did_count,
        "created_at": t.created_at,
        "updated_at": t.updated_at,
    }


@router.get("", response_model=List[Dict[str, Any]])
async def list_trunks(
    carrier_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """List all trunks, optionally filtered by carrier or status."""
    stmt = select(SIPTrunk).order_by(SIPTrunk.priority.asc(), SIPTrunk.created_at.desc())
    if carrier_id:
        stmt = stmt.where(SIPTrunk.carrier_id == carrier_id)
    if status_filter:
        stmt = stmt.where(SIPTrunk.status == status_filter.upper())

    trunks = (await db.scalars(stmt)).all()

    results = []
    for t in trunks:
        carrier = await db.get(SIPCarrier, t.carrier_id)
        d_stmt = select(DIDNumber).where(DIDNumber.trunk_id == t.id)
        dids = (await db.scalars(d_stmt)).all()
        results.append(_trunk_to_out(t, carrier, len(dids)))

    return results


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_trunk(
    payload: TrunkCreatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Create a new SIP trunk associated with a carrier."""
    carrier = await db.get(SIPCarrier, payload.carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="Parent SIP Carrier not found")

    trunk = SIPTrunk(
        carrier_id=payload.carrier_id,
        business_id=tenant_id,
        name=payload.name.strip(),
        direction=payload.direction.upper(),
        max_concurrency=payload.max_concurrency,
        priority=payload.priority,
        proxy_server=payload.proxy_server.strip() if payload.proxy_server else None,
        register_enabled=payload.register_enabled,
        registration_status="REGISTERED" if payload.register_enabled else "PEER_ACTIVE",
        status=payload.status.upper(),
    )
    db.add(trunk)
    await db.commit()
    await db.refresh(trunk)

    return _trunk_to_out(trunk, carrier, did_count=0)


@router.get("/{trunk_id}", response_model=Dict[str, Any])
async def get_trunk(
    trunk_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """Get single SIP trunk by ID."""
    trunk = await db.get(SIPTrunk, trunk_id)
    if not trunk:
        raise HTTPException(status_code=404, detail="SIP Trunk not found")

    carrier = await db.get(SIPCarrier, trunk.carrier_id)
    d_stmt = select(DIDNumber).where(DIDNumber.trunk_id == trunk.id)
    dids = (await db.scalars(d_stmt)).all()

    return _trunk_to_out(trunk, carrier, len(dids))


@router.put("/{trunk_id}", response_model=Dict[str, Any])
async def update_trunk(
    trunk_id: str,
    payload: TrunkUpdatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Update SIP trunk settings."""
    trunk = await db.get(SIPTrunk, trunk_id)
    if not trunk:
        raise HTTPException(status_code=404, detail="SIP Trunk not found")

    if payload.name is not None:
        trunk.name = payload.name.strip()
    if payload.direction is not None:
        trunk.direction = payload.direction.upper()
    if payload.max_concurrency is not None:
        trunk.max_concurrency = payload.max_concurrency
    if payload.priority is not None:
        trunk.priority = payload.priority
    if payload.proxy_server is not None:
        trunk.proxy_server = payload.proxy_server.strip() if payload.proxy_server else None
    if payload.register_enabled is not None:
        trunk.register_enabled = payload.register_enabled
    if payload.registration_status is not None:
        trunk.registration_status = payload.registration_status.upper()
    if payload.status is not None:
        trunk.status = payload.status.upper()

    trunk.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(trunk)
    await db.commit()
    await db.refresh(trunk)

    carrier = await db.get(SIPCarrier, trunk.carrier_id)
    d_stmt = select(DIDNumber).where(DIDNumber.trunk_id == trunk.id)
    dids = (await db.scalars(d_stmt)).all()

    return _trunk_to_out(trunk, carrier, len(dids))


@router.delete("/{trunk_id}")
async def delete_trunk(
    trunk_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Delete a SIP trunk."""
    trunk = await db.get(SIPTrunk, trunk_id)
    if not trunk:
        raise HTTPException(status_code=404, detail="SIP Trunk not found")

    await db.delete(trunk)
    await db.commit()
    return {"success": True, "message": f"Trunk '{trunk.name}' deleted successfully"}


@router.post("/{trunk_id}/test-connection")
async def test_trunk_connection(
    trunk_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """Tests the underlying carrier connection for this trunk."""
    trunk = await db.get(SIPTrunk, trunk_id)
    if not trunk:
        raise HTTPException(status_code=404, detail="SIP Trunk not found")

    carrier = await db.get(SIPCarrier, trunk.carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="Carrier for this trunk not found")

    probe_result = await sip_service.test_carrier_connection(carrier)
    await sip_service.record_health_result(db, carrier.id, probe_result)

    return {
        "success": probe_result.get("success", False),
        "trunk_id": trunk.id,
        "trunk_name": trunk.name,
        "carrier_name": carrier.name,
        "sip_host": carrier.sip_host,
        "status": probe_result.get("status"),
        "latency_ms": probe_result.get("latency_ms"),
        "sip_code": probe_result.get("sip_code"),
        "sip_reason": probe_result.get("sip_reason"),
        "error": probe_result.get("error"),
    }
