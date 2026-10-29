"""
SIP Carrier Management API Routes
Provides CRUD, live Asterisk socket health diagnostics, and failover policy configuration.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_db_user, get_current_tenant_id, require_admin
from app.db.database import get_db
from app.db.models import (
    CarrierHealthLog,
    CarrierRoutingPolicy,
    SIPCarrier,
    SIPTrunk,
    DIDNumber,
    User,
)
from app.services.sip_service import sip_service

router = APIRouter(prefix="/api/sip-carriers", tags=["sip_carriers"])


# --- Schemas ---

class CarrierCreatePayload(BaseModel):
    name: str
    provider_type: str = "ASTERISK"  # ASTERISK, TWILIO, FREEPBX, GENERIC_SIP, TATA, AIRTEL, JIO
    sip_host: str
    sip_port: int = 5060
    transport: str = "UDP"  # UDP, TCP, TLS
    auth_type: str = "CREDENTIAL"  # CREDENTIAL, IP_BASED
    username: Optional[str] = None
    password: Optional[str] = None
    inbound_enabled: bool = True
    outbound_enabled: bool = True
    max_channels: int = 30
    status: str = "ACTIVE"
    settings: Optional[Dict[str, Any]] = None


class CarrierUpdatePayload(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[str] = None
    sip_host: Optional[str] = None
    sip_port: Optional[int] = None
    transport: Optional[str] = None
    auth_type: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None  # If provided and not masked, updates password_hash
    inbound_enabled: Optional[bool] = None
    outbound_enabled: Optional[bool] = None
    max_channels: Optional[int] = None
    status: Optional[str] = None
    settings: Optional[Dict[str, Any]] = None


class CarrierOut(BaseModel):
    id: str
    business_id: Optional[str] = None
    name: str
    provider_type: str
    sip_host: str
    sip_port: int
    transport: str
    auth_type: str
    username: Optional[str] = None
    has_password: bool = False
    inbound_enabled: bool = True
    outbound_enabled: bool = True
    max_channels: int = 30
    status: str
    last_health_status: str
    last_health_latency_ms: Optional[float] = None
    last_health_checked_at: Optional[datetime] = None
    last_error_message: Optional[str] = None
    trunk_count: int = 0
    did_count: int = 0
    created_at: datetime
    updated_at: datetime


class RoutingPolicyPayload(BaseModel):
    primary_carrier_id: str
    secondary_carrier_id: Optional[str] = None
    campaign_id: Optional[str] = None
    max_retry_attempts: int = 2
    retry_on_status_codes: Optional[List[str]] = None


def _carrier_to_out(c: SIPCarrier, trunk_count: int = 0, did_count: int = 0) -> Dict[str, Any]:
    return {
        "id": c.id,
        "business_id": c.business_id,
        "name": c.name,
        "provider_type": c.provider_type,
        "sip_host": c.sip_host,
        "sip_port": c.sip_port,
        "transport": c.transport,
        "auth_type": c.auth_type,
        "username": c.username,
        "has_password": bool(c.password_hash and len(c.password_hash) > 0),
        "inbound_enabled": c.inbound_enabled,
        "outbound_enabled": c.outbound_enabled,
        "max_channels": c.max_channels,
        "status": c.status,
        "last_health_status": c.last_health_status or "UNKNOWN",
        "last_health_latency_ms": c.last_health_latency_ms,
        "last_health_checked_at": c.last_health_checked_at,
        "last_error_message": c.last_error_message,
        "trunk_count": trunk_count,
        "did_count": did_count,
        "created_at": c.created_at,
        "updated_at": c.updated_at,
    }


# --- Routes ---

@router.get("", response_model=List[Dict[str, Any]])
async def list_carriers(
    status_filter: Optional[str] = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """List all SIP carriers for the tenant/admin."""
    stmt = select(SIPCarrier).order_by(SIPCarrier.created_at.desc())
    if status_filter:
        stmt = stmt.where(SIPCarrier.status == status_filter.upper())

    carriers = (await db.scalars(stmt)).all()

    # Enrich with trunk and DID counts
    results = []
    for c in carriers:
        t_stmt = select(SIPTrunk).where(SIPTrunk.carrier_id == c.id)
        trunks = (await db.scalars(t_stmt)).all()
        
        d_stmt = select(DIDNumber).where(DIDNumber.carrier_id == c.id)
        dids = (await db.scalars(d_stmt)).all()

        results.append(_carrier_to_out(c, len(trunks), len(dids)))

    return results


@router.post("", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_carrier(
    payload: CarrierCreatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Create a new SIP carrier configuration."""
    carrier = SIPCarrier(
        business_id=tenant_id,
        name=payload.name.strip(),
        provider_type=payload.provider_type.upper(),
        sip_host=payload.sip_host.strip(),
        sip_port=payload.sip_port,
        transport=payload.transport.upper(),
        auth_type=payload.auth_type.upper(),
        username=payload.username.strip() if payload.username else None,
        password_hash=payload.password if payload.password else None,
        inbound_enabled=payload.inbound_enabled,
        outbound_enabled=payload.outbound_enabled,
        max_channels=payload.max_channels,
        status=payload.status.upper(),
        settings=payload.settings or {},
    )
    db.add(carrier)
    await db.commit()
    await db.refresh(carrier)

    # Automatically create a default primary trunk for this carrier
    default_trunk = SIPTrunk(
        carrier_id=carrier.id,
        business_id=tenant_id,
        name=f"{carrier.name} - Primary Trunk",
        direction="BIDIRECTIONAL",
        max_concurrency=carrier.max_channels,
        priority=1,
        status="ACTIVE",
    )
    db.add(default_trunk)
    await db.commit()

    return _carrier_to_out(carrier, trunk_count=1, did_count=0)


@router.get("/{carrier_id}", response_model=Dict[str, Any])
async def get_carrier(
    carrier_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """Retrieve details for a specific carrier."""
    carrier = await db.get(SIPCarrier, carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="SIP Carrier not found")

    t_stmt = select(SIPTrunk).where(SIPTrunk.carrier_id == carrier.id)
    trunks = (await db.scalars(t_stmt)).all()
    d_stmt = select(DIDNumber).where(DIDNumber.carrier_id == carrier.id)
    dids = (await db.scalars(d_stmt)).all()

    return _carrier_to_out(carrier, len(trunks), len(dids))


@router.put("/{carrier_id}", response_model=Dict[str, Any])
async def update_carrier(
    carrier_id: str,
    payload: CarrierUpdatePayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Update carrier configuration."""
    carrier = await db.get(SIPCarrier, carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="SIP Carrier not found")

    if payload.name is not None:
        carrier.name = payload.name.strip()
    if payload.provider_type is not None:
        carrier.provider_type = payload.provider_type.upper()
    if payload.sip_host is not None:
        carrier.sip_host = payload.sip_host.strip()
    if payload.sip_port is not None:
        carrier.sip_port = payload.sip_port
    if payload.transport is not None:
        carrier.transport = payload.transport.upper()
    if payload.auth_type is not None:
        carrier.auth_type = payload.auth_type.upper()
    if payload.username is not None:
        carrier.username = payload.username.strip() if payload.username else None
    if payload.password is not None and payload.password.strip() and "••••" not in payload.password:
        carrier.password_hash = payload.password.strip()
    if payload.inbound_enabled is not None:
        carrier.inbound_enabled = payload.inbound_enabled
    if payload.outbound_enabled is not None:
        carrier.outbound_enabled = payload.outbound_enabled
    if payload.max_channels is not None:
        carrier.max_channels = payload.max_channels
    if payload.status is not None:
        carrier.status = payload.status.upper()
    if payload.settings is not None:
        carrier.settings = payload.settings

    carrier.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(carrier)
    await db.commit()
    await db.refresh(carrier)

    t_stmt = select(SIPTrunk).where(SIPTrunk.carrier_id == carrier.id)
    trunks = (await db.scalars(t_stmt)).all()
    d_stmt = select(DIDNumber).where(DIDNumber.carrier_id == carrier.id)
    dids = (await db.scalars(d_stmt)).all()

    return _carrier_to_out(carrier, len(trunks), len(dids))


@router.delete("/{carrier_id}")
async def delete_carrier(
    carrier_id: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Delete a SIP carrier and its associated trunks."""
    carrier = await db.get(SIPCarrier, carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="SIP Carrier not found")

    await db.delete(carrier)
    await db.commit()
    return {"success": True, "message": f"Carrier '{carrier.name}' deleted successfully"}


@router.post("/{carrier_id}/test-connection")
async def test_carrier_connection_endpoint(
    carrier_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """
    Executes live socket test (OPTIONS/REGISTER probe) against carrier's SIP host:port.
    Records probe result in CarrierHealthLog and updates last_health_* fields.
    """
    carrier = await db.get(SIPCarrier, carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="SIP Carrier not found")

    # Run socket probe
    probe_result = await sip_service.test_carrier_connection(carrier)

    # Persist health log
    await sip_service.record_health_result(db, carrier_id, probe_result)

    return {
        "success": probe_result.get("success", False),
        "carrier_id": carrier.id,
        "carrier_name": carrier.name,
        "sip_host": carrier.sip_host,
        "sip_port": carrier.sip_port,
        "status": probe_result.get("status"),
        "latency_ms": probe_result.get("latency_ms"),
        "sip_code": probe_result.get("sip_code"),
        "sip_reason": probe_result.get("sip_reason"),
        "user_agent": probe_result.get("user_agent"),
        "error": probe_result.get("error"),
    }


@router.post("/{carrier_id}/status")
async def toggle_carrier_status(
    carrier_id: str,
    status_value: str = Query(..., description="ACTIVE, INACTIVE, or MAINTENANCE"),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Quickly toggle carrier status."""
    carrier = await db.get(SIPCarrier, carrier_id)
    if not carrier:
        raise HTTPException(status_code=404, detail="SIP Carrier not found")

    val = status_value.upper()
    if val not in ("ACTIVE", "INACTIVE", "MAINTENANCE"):
        raise HTTPException(status_code=400, detail="Invalid status. Must be ACTIVE, INACTIVE, or MAINTENANCE")

    carrier.status = val
    carrier.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.add(carrier)
    await db.commit()

    return {"success": True, "carrier_id": carrier.id, "status": carrier.status}


@router.get("/{carrier_id}/health-logs")
async def get_carrier_health_logs(
    carrier_id: str,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
):
    """Retrieve historical health probe logs for this carrier."""
    stmt = (
        select(CarrierHealthLog)
        .where(CarrierHealthLog.carrier_id == carrier_id)
        .order_by(CarrierHealthLog.checked_at.desc())
        .limit(limit)
    )
    logs = (await db.scalars(stmt)).all()
    return logs


# --- Failover Routing Policies ---

@router.get("/routing-policies/current")
async def get_routing_policy(
    campaign_id: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Get current active failover routing policy."""
    stmt = select(CarrierRoutingPolicy).where(CarrierRoutingPolicy.business_id == tenant_id)
    if campaign_id:
        stmt = stmt.where(CarrierRoutingPolicy.campaign_id == campaign_id)
    else:
        stmt = stmt.where(CarrierRoutingPolicy.campaign_id == None)

    policy = (await db.scalars(stmt)).first()
    if not policy:
        # Return fallback default
        c_stmt = select(SIPCarrier).where(SIPCarrier.status == "ACTIVE").order_by(SIPCarrier.created_at.asc())
        carriers = (await db.scalars(c_stmt)).all()
        return {
            "id": None,
            "business_id": tenant_id,
            "campaign_id": campaign_id,
            "primary_carrier_id": carriers[0].id if len(carriers) > 0 else None,
            "secondary_carrier_id": carriers[1].id if len(carriers) > 1 else None,
            "max_retry_attempts": 2,
            "retry_on_status_codes": ["503", "486", "408", "TIMEOUT", "CONGESTION"],
        }
    return policy


@router.post("/routing-policies")
async def save_routing_policy(
    payload: RoutingPolicyPayload,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
    tenant_id: str = Depends(get_current_tenant_id),
):
    """Create or update failover routing policy for tenant or specific campaign."""
    stmt = select(CarrierRoutingPolicy).where(CarrierRoutingPolicy.business_id == tenant_id)
    if payload.campaign_id:
        stmt = stmt.where(CarrierRoutingPolicy.campaign_id == payload.campaign_id)
    else:
        stmt = stmt.where(CarrierRoutingPolicy.campaign_id == None)

    policy = (await db.scalars(stmt)).first()
    if not policy:
        policy = CarrierRoutingPolicy(
            business_id=tenant_id,
            campaign_id=payload.campaign_id,
            primary_carrier_id=payload.primary_carrier_id,
            secondary_carrier_id=payload.secondary_carrier_id,
            max_retry_attempts=payload.max_retry_attempts,
            retry_on_status_codes=payload.retry_on_status_codes or ["503", "486", "408", "TIMEOUT", "CONGESTION"],
        )
        db.add(policy)
    else:
        policy.primary_carrier_id = payload.primary_carrier_id
        policy.secondary_carrier_id = payload.secondary_carrier_id
        policy.max_retry_attempts = payload.max_retry_attempts
        if payload.retry_on_status_codes:
            policy.retry_on_status_codes = payload.retry_on_status_codes
        policy.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(policy)

    await db.commit()
    await db.refresh(policy)
    return policy
