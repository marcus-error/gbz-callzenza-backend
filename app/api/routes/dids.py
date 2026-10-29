"""
DID Number Inventory API Routes
Manages Direct Inward Dialing (DID) telephone numbers, carrier/trunk associations,
and campaign/agent routing assignments.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_db_user, get_current_tenant_id, require_admin
from app.db.database import get_db
from app.db.models import (
    AIAgent,
    Campaign,
    DIDNumber,
    Expert,
    InboundNumber,
    SIPCarrier,
    SIPTrunk,
    User,
    UserGroup,
)

router = APIRouter(prefix="/api/dids", tags=["dids"])


class DIDCreatePayload(BaseModel):
    phone_number: str
    name: Optional[str] = None
    sip_account_id: Optional[str] = None
    carrier_id: Optional[str] = None
    trunk_id: Optional[str] = None
    assigned_campaign_id: Optional[str] = None
    routing_type: str = "AI_AGENT"  # AI_AGENT, HUMAN_AGENT, USER_GROUP, IVR, UNASSIGNED
    assigned_agent_id: Optional[str] = None
    assigned_voice_agent_id: Optional[str] = None
    assigned_group_id: Optional[str] = None
    is_outbound_caller_id: bool = True
    status: str = "ACTIVE"  # ACTIVE, UNASSIGNED, SUSPENDED


class DIDUpdatePayload(BaseModel):
    phone_number: Optional[str] = None
    name: Optional[str] = None
    sip_account_id: Optional[str] = None
    carrier_id: Optional[str] = None
    trunk_id: Optional[str] = None
    assigned_campaign_id: Optional[str] = None
    routing_type: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    assigned_voice_agent_id: Optional[str] = None
    assigned_group_id: Optional[str] = None
    is_outbound_caller_id: Optional[bool] = None
    status: Optional[str] = None


class DIDAssignPayload(BaseModel):
    assigned_campaign_id: Optional[str] = None
    routing_type: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    assigned_voice_agent_id: Optional[str] = None
    assigned_group_id: Optional[str] = None


async def _enrich_did(d: DIDNumber, db: AsyncSession) -> Dict[str, Any]:
    carrier_name = None
    carrier_host = None
    if d.carrier_id:
        c = await db.get(SIPCarrier, d.carrier_id)
        if c:
            carrier_name = c.name
            carrier_host = c.sip_host

    trunk_name = None
    if d.trunk_id:
        t = await db.get(SIPTrunk, d.trunk_id)
        if t:
            trunk_name = t.name

    sip_account_name = None
    if getattr(d, "sip_account_id", None):
        from app.db.models import SIPAccount
        s_acc = await db.get(SIPAccount, d.sip_account_id)
        if s_acc:
            sip_account_name = s_acc.name

    campaign_name = None
    if d.assigned_campaign_id:
        camp = await db.get(Campaign, d.assigned_campaign_id)
        if camp:
            campaign_name = camp.name

    agent_name = None
    if d.assigned_agent_id:
        ag = await db.get(AIAgent, d.assigned_agent_id)
        if ag:
            agent_name = ag.name
    elif d.assigned_voice_agent_id:
        exp = await db.get(Expert, d.assigned_voice_agent_id)
        if exp:
            agent_name = exp.name

    group_name = None
    if d.assigned_group_id:
        grp = await db.get(UserGroup, d.assigned_group_id)
        if grp:
            group_name = grp.name

    return {
        "id": d.id,
        "business_id": d.business_id,
        "phone_number": d.phone_number,
        "name": d.name,
        "sip_account_id": getattr(d, "sip_account_id", None),
        "sip_account_name": sip_account_name,
        "carrier_id": d.carrier_id,
        "carrier_name": carrier_name,
        "carrier_host": carrier_host,
        "trunk_id": d.trunk_id,
        "trunk_name": trunk_name,
        "assigned_campaign_id": d.assigned_campaign_id,
        "campaign_name": campaign_name,
        "routing_type": d.routing_type,
        "assigned_agent_id": d.assigned_agent_id,
        "assigned_voice_agent_id": d.assigned_voice_agent_id,
        "agent_name": agent_name,
        "assigned_group_id": d.assigned_group_id,
        "group_name": group_name,
        "is_outbound_caller_id": d.is_outbound_caller_id,
        "status": d.status,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


@router.get("", response_model=List[Dict[str, Any]])
async def list_dids(
    carrier_id: Optional[str] = Query(None),
    trunk_id: Optional[str] = Query(None),
    sip_account_id: Optional[str] = Query(None),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """List all DIDs with associated carrier, trunk, and campaign details."""
    settings = get_settings()
    configured_did = (getattr(settings, "sip_did_number", None) or getattr(settings, "sip_username", None) or "").strip()
    
    # Ensure the configured primary DID from environment/settings exists and is active
    if configured_did:
        existing_cfg = (await db.scalars(
            select(DIDNumber).where(
                or_(
                    DIDNumber.phone_number == configured_did,
                    DIDNumber.phone_number == f"+91{configured_did}",
                    DIDNumber.phone_number == f"+{configured_did}",
                )
            )
        )).first()
        if not existing_cfg:
            c_default = (await db.scalars(select(SIPCarrier).where(SIPCarrier.status == "ACTIVE").order_by(SIPCarrier.created_at.asc()))).first()
            t_default = (await db.scalars(select(SIPTrunk).where(SIPTrunk.carrier_id == (c_default.id if c_default else None)))).first() if c_default else None
            new_did = DIDNumber(
                business_id=getattr(current_user, "business_id", None) or "c3dce06c-8330-4724-ba33-b5282b3c3596",
                phone_number=configured_did,
                name=f"Primary Asterisk PBX Outbound DID ({configured_did})",
                carrier_id=c_default.id if c_default else None,
                trunk_id=t_default.id if t_default else None,
                routing_type="AI_AGENT",
                is_outbound_caller_id=True,
                status="ACTIVE",
            )
            db.add(new_did)
            await db.commit()

    stmt = select(DIDNumber).order_by(DIDNumber.created_at.desc())
    if sip_account_id:
        stmt = stmt.where(DIDNumber.sip_account_id == sip_account_id)
    if carrier_id:
        stmt = stmt.where(or_(DIDNumber.carrier_id == carrier_id, DIDNumber.carrier_id == None))
    if trunk_id:
        stmt = stmt.where(DIDNumber.trunk_id == trunk_id)
    if status_filter:
        if status_filter.upper() == "ACTIVE":
            stmt = stmt.where(or_(DIDNumber.status == "ACTIVE", DIDNumber.status == "active", DIDNumber.status == None))
        else:
            stmt = stmt.where(DIDNumber.status == status_filter.upper())

    dids = (await db.scalars(stmt)).all()
    results = []
    for d in dids:
        results.append(await _enrich_did(d, db))

    return results


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_did(
    payload: DIDCreatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Register a new DID in the inventory."""
    phone_clean = payload.phone_number.strip()
    # Check if exists
    stmt = select(DIDNumber).where(DIDNumber.phone_number == phone_clean)
    existing = (await db.scalars(stmt)).first()
    if existing:
        raise HTTPException(status_code=400, detail=f"DID {phone_clean} is already registered in inventory")

    did = DIDNumber(
        business_id=tenant_id,
        phone_number=phone_clean,
        name=payload.name.strip() if payload.name else None,
        sip_account_id=payload.sip_account_id,
        carrier_id=payload.carrier_id,
        trunk_id=payload.trunk_id,
        assigned_campaign_id=payload.assigned_campaign_id,
        routing_type=payload.routing_type.upper(),
        assigned_agent_id=payload.assigned_agent_id,
        assigned_voice_agent_id=payload.assigned_voice_agent_id,
        assigned_group_id=payload.assigned_group_id,
        is_outbound_caller_id=payload.is_outbound_caller_id,
        status=payload.status.upper(),
    )
    db.add(did)
    await db.commit()
    await db.refresh(did)

    return await _enrich_did(did, db)


@router.get("/{did_id}", response_model=Dict[str, Any])
async def get_did(
    did_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """Retrieve details of a single DID."""
    did = await db.get(DIDNumber, did_id)
    if not did:
        raise HTTPException(status_code=404, detail="DID Number not found")
    return await _enrich_did(did, db)


@router.put("/{did_id}", response_model=Dict[str, Any])
async def update_did(
    did_id: str,
    payload: DIDUpdatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Update DID configuration and assignments."""
    did = await db.get(DIDNumber, did_id)
    if not did:
        raise HTTPException(status_code=404, detail="DID Number not found")

    if payload.phone_number is not None:
        did.phone_number = payload.phone_number.strip()
    if payload.name is not None:
        did.name = payload.name.strip() if payload.name else None
    if payload.sip_account_id is not None:
        did.sip_account_id = payload.sip_account_id if payload.sip_account_id else None
    if payload.carrier_id is not None:
        did.carrier_id = payload.carrier_id if payload.carrier_id else None
    if payload.trunk_id is not None:
        did.trunk_id = payload.trunk_id if payload.trunk_id else None
    if payload.assigned_campaign_id is not None:
        did.assigned_campaign_id = payload.assigned_campaign_id if payload.assigned_campaign_id else None
    if payload.routing_type is not None:
        did.routing_type = payload.routing_type.upper()
    if payload.assigned_agent_id is not None:
        did.assigned_agent_id = payload.assigned_agent_id if payload.assigned_agent_id else None
    if payload.assigned_voice_agent_id is not None:
        did.assigned_voice_agent_id = payload.assigned_voice_agent_id if payload.assigned_voice_agent_id else None
    if payload.assigned_group_id is not None:
        did.assigned_group_id = payload.assigned_group_id if payload.assigned_group_id else None
    if payload.is_outbound_caller_id is not None:
        did.is_outbound_caller_id = payload.is_outbound_caller_id
    if payload.status is not None:
        did.status = payload.status.upper()

    did.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(did)
    await db.commit()
    await db.refresh(did)

    return await _enrich_did(did, db)


@router.delete("/{did_id}")
async def delete_did(
    did_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Delete a DID from inventory."""
    did = await db.get(DIDNumber, did_id)
    if not did:
        raise HTTPException(status_code=404, detail="DID Number not found")

    await db.delete(did)
    await db.commit()
    return {"success": True, "message": f"DID '{did.phone_number}' deleted successfully"}


@router.post("/{did_id}/assign", response_model=Dict[str, Any])
async def assign_did(
    did_id: str,
    payload: DIDAssignPayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Quick assign a DID to a campaign, agent, or group."""
    did = await db.get(DIDNumber, did_id)
    if not did:
        raise HTTPException(status_code=404, detail="DID Number not found")

    if payload.assigned_campaign_id is not None:
        did.assigned_campaign_id = payload.assigned_campaign_id or None
    if payload.routing_type is not None:
        did.routing_type = payload.routing_type.upper()
    if payload.assigned_agent_id is not None:
        did.assigned_agent_id = payload.assigned_agent_id or None
    if payload.assigned_voice_agent_id is not None:
        did.assigned_voice_agent_id = payload.assigned_voice_agent_id or None
    if payload.assigned_group_id is not None:
        did.assigned_group_id = payload.assigned_group_id or None

    did.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(did)
    await db.commit()
    await db.refresh(did)

    return await _enrich_did(did, db)


@router.post("/sync-inbound")
async def sync_inbound_numbers(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """
    Scans InboundNumber table and imports any numbers into did_inventory.
    Ensures existing inbound routes and new carrier DIDs operate seamlessly.
    """
    inbound_stmt = select(InboundNumber)
    inbound_numbers = (await db.scalars(inbound_stmt)).all()

    # Find default carrier if available
    c_stmt = select(SIPCarrier).where(SIPCarrier.status == "ACTIVE").order_by(SIPCarrier.created_at.asc())
    default_carrier = (await db.scalars(c_stmt)).first()

    synced_count = 0
    for num in inbound_numbers:
        # Check if already in DID inventory
        check_stmt = select(DIDNumber).where(DIDNumber.phone_number == num.phone_number)
        exists = (await db.scalars(check_stmt)).first()
        if not exists:
            new_did = DIDNumber(
                business_id=tenant_id,
                phone_number=num.phone_number,
                name=num.name or f"DID {num.phone_number}",
                carrier_id=default_carrier.id if default_carrier else None,
                routing_type=num.routing_type or "AI_AGENT",
                assigned_agent_id=num.assigned_ai_agent_id,
                assigned_voice_agent_id=num.assigned_agent_id,
                assigned_group_id=num.assigned_group_id,
                is_outbound_caller_id=True,
                status=num.status or "ACTIVE",
            )
            db.add(new_did)
            synced_count += 1

    if synced_count > 0:
        await db.commit()

    return {"success": True, "synced_count": synced_count}
