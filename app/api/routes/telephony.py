from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.websockets import dispatch_telephony_event
from app.core.config import get_settings
from app.core.deps import get_current_db_user, get_current_tenant_id
from app.db.database import get_db
from app.db.models import AIAgent, Call, Campaign, User

router = APIRouter(prefix="/api/telephony", tags=["telephony"])
settings = get_settings()


# ---------------------------------------------------------------------------
# Request & Response Schemas
# ---------------------------------------------------------------------------

class SipConfigResponse(BaseModel):
    sipServer: str
    sipPort: int
    sipWsUrl: str
    sipDomain: str
    extension: str
    username: str
    displayName: str
    iceServers: List[Dict[str, Any]]
    sipToken: Optional[str] = None


class TelephonyOutboundRequest(BaseModel):
    phone_number: str = Field(..., description="Destination phone number (e.g. +91XXXXXXXXXX)")
    agent_id: Optional[str] = Field(None, description="Optional AI/Human Agent ID")
    campaign_id: Optional[str] = Field(None, description="Optional Campaign ID")


class TelephonyInboundRequest(BaseModel):
    caller_number: str = Field(..., description="Incoming caller phone number")
    destination_extension: str = Field("1001", description="Target SIP extension (e.g. 1001 or 1002)")
    tenant_id: Optional[str] = Field(None, description="Optional tenant/business ID")
    agent_id: Optional[str] = Field(None, description="Optional agent ID")


class CallResponse(BaseModel):
    id: str
    tenant_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    campaign_id: Optional[str] = None
    direction: str
    caller_number: str
    destination_number: str
    sip_extension: Optional[str] = None
    status: str
    started_at: Optional[str] = None
    answered_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration: int = 0
    hangup_reason: Optional[str] = None
    recording_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers & Phone Number Validation
# ---------------------------------------------------------------------------

def validate_phone_number(phone: str) -> str:
    """Validates and cleans phone numbers. Rejects empty or invalid strings."""
    if not phone or not isinstance(phone, str):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Destination phone number is required and cannot be empty.",
        )
    cleaned = re.sub(r"[\s\-\(\)\.]+", "", phone.strip())
    # Require at least 3 digits (allows internal extensions e.g. 1001, 1002 as well as external numbers)
    if not re.match(r"^\+?[0-9]{3,20}$", cleaned):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid phone number format: '{phone}'. Must contain between 3 and 20 numeric digits.",
        )
    return cleaned


def serialize_call(call: Call) -> Dict[str, Any]:
    return {
        "id": call.id,
        "tenant_id": call.business_id,
        "business_id": call.business_id,
        "user_id": call.user_id,
        "agent_id": call.agent_id,
        "campaign_id": call.campaign_id,
        "direction": (call.direction or "outbound").lower(),
        "caller_number": call.from_number or "",
        "destination_number": call.to_number or "",
        "sip_extension": call.sip_extension or "",
        "status": (call.status or "initiated").lower(),
        "started_at": call.initiated_at.isoformat() if call.initiated_at else None,
        "answered_at": call.connected_at.isoformat() if call.connected_at else None,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
        "duration": call.duration_seconds or 0,
        "hangup_reason": call.hangup_reason,
        "recording_url": call.recording_url or "",
    }


# ---------------------------------------------------------------------------
# 1. SIP Configuration
# ---------------------------------------------------------------------------

@router.get("/sip/config", response_model=SipConfigResponse)
async def get_sip_config(
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns SIP & WebRTC configuration for the authenticated user/extension from the database.
    Never exposes plaintext SIP passwords to the client.
    """
    from app.db.models import SIPAccount
    stmt = select(SIPAccount).where(
        SIPAccount.organization_id == tenant_id,
        SIPAccount.enabled == True,
    )
    accounts = (await db.execute(stmt)).scalars().all()
    default_acc = next((a for a in accounts if getattr(a, "is_default", False) or (isinstance(getattr(a, "settings", None), dict) and a.settings.get("is_default"))), None)
    sip_acc = default_acc or (accounts[0] if accounts else None)

    extension = current_user.sip_extension or (sip_acc.extension if sip_acc and getattr(sip_acc, "extension", None) else "1001")
    username = current_user.sip_extension or (sip_acc.username if sip_acc else "1001")
    display_name = (
        current_user.full_name
        or f"{current_user.first_name} {current_user.last_name}".strip()
        or (sip_acc.display_name if sip_acc and getattr(sip_acc, "display_name", None) else f"Agent {extension}")
    )

    sip_server = sip_acc.host if sip_acc else ""
    sip_port = int(sip_acc.port or 5060) if sip_acc else 5060
    sip_ws_url = (sip_acc.sip_ws_url if sip_acc and getattr(sip_acc, "sip_ws_url", None) else "") or (f"wss://{sip_server}:8089/ws" if sip_server else "")
    sip_domain = sip_server

    stun_host = (sip_acc.stun_server if sip_acc and getattr(sip_acc, "stun_server", None) else None) or settings.stun_server or "stun:stun.l.google.com:19302"
    ice_servers: List[Dict[str, Any]] = [
        {"urls": [stun_host]}
    ]
    if settings.turn_server:
        turn_entry: Dict[str, Any] = {"urls": [settings.turn_server]}
        if settings.turn_username:
            turn_entry["username"] = settings.turn_username
        if settings.turn_password:
            turn_entry["credential"] = settings.turn_password
        ice_servers.append(turn_entry)

    logger.info(
        f"[SIP REGISTRATION] Generated dynamic SIP config for tenant_id={tenant_id}, "
        f"user_id={current_user.id}, extension={extension}, sip_server={sip_server}"
    )

    return SipConfigResponse(
        sipServer=sip_server,
        sipPort=sip_port,
        sipWsUrl=sip_ws_url,
        sipDomain=sip_domain,
        extension=extension,
        username=username,
        displayName=display_name,
        iceServers=ice_servers,
        sipToken=None,
    )


@router.get("/sip/daemon/status")
async def get_sip_daemon_status():
    from app.services import sip_inbound_daemon
    account = sip_inbound_daemon.ACTIVE_INBOUND_ACCOUNT
    return {
        "running": sip_inbound_daemon.DAEMON_RUNNING,
        "registered": sip_inbound_daemon.IS_REGISTERED,
        "sip_server": sip_inbound_daemon.CURRENT_REGISTERED_HOST or (account.host if account else ""),
        "username": sip_inbound_daemon.CURRENT_REGISTERED_USER or (account.username if account else ""),
        "account_name": getattr(account, "name", None) if account else None,
        "account_id": getattr(account, "id", None) if account else None,
    }


@router.post("/sip/daemon/start")
async def start_sip_daemon_endpoint():
    from app.services.sip_inbound_daemon import start_sip_daemon, _get_listen_port
    start_sip_daemon()
    return {
        "success": True,
        "message": f"SIP daemon started on UDP port {_get_listen_port()}",
    }


@router.post("/sip/daemon/stop")
async def stop_sip_daemon_endpoint():
    from app.services.sip_inbound_daemon import stop_sip_daemon
    stop_sip_daemon()
    return {
        "success": True,
        "message": "SIP daemon stopped and unregistered from PBX.",
    }


# ---------------------------------------------------------------------------
# 2. Outbound Call
# ---------------------------------------------------------------------------

@router.post("/calls/outbound", status_code=status.HTTP_201_CREATED)
async def initiate_outbound_call(
    payload: TelephonyOutboundRequest,
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Validates tenant, agent, campaign, and destination number.
    Enforces rate-limiting / duplicate call prevention.
    Creates Call record and broadcasts 'call_ringing' WebSocket event.
    """
    destination_number = validate_phone_number(payload.phone_number)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Validate Agent if provided
    if payload.agent_id:
        agent = await db.get(AIAgent, payload.agent_id)
        if agent and agent.business_id and agent.business_id != tenant_id:
            logger.warning(
                f"[SECURITY] Tenant mismatch on agent validation: "
                f"tenant_id={tenant_id} != agent.business_id={agent.business_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Agent does not belong to your organization.",
            )

    # Validate Campaign if provided
    if payload.campaign_id:
        campaign = await db.get(Campaign, payload.campaign_id)
        if campaign and campaign.business_id and campaign.business_id != tenant_id:
            logger.warning(
                f"[SECURITY] Tenant mismatch on campaign validation: "
                f"tenant_id={tenant_id} != campaign.business_id={campaign.business_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Campaign does not belong to your organization.",
            )

    # Duplicate call prevention: check active calls to same number within tenant
    active_dup_stmt = select(Call).where(
        Call.business_id == tenant_id,
        Call.to_number == destination_number,
        Call.status.in_(["initiated", "ringing", "answered", "connected", "QUEUED", "RINGING", "IN_PROGRESS"]),
    )
    existing_call = (await db.execute(active_dup_stmt)).scalars().first()
    if existing_call:
        logger.warning(
            f"[OUTBOUND CALL REJECTED] Duplicate call attempt to {destination_number} "
            f"for tenant {tenant_id}. Existing call ID: {existing_call.id}"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active call to '{destination_number}' is already in progress (Call ID: {existing_call.id}).",
        )

    extension = current_user.sip_extension or "1001"
    call_id = str(uuid.uuid4())

    new_call = Call(
        id=call_id,
        business_id=tenant_id,
        user_id=current_user.id,
        agent_id=payload.agent_id,
        campaign_id=payload.campaign_id,
        direction="outbound",
        from_number=extension,
        to_number=destination_number,
        sip_extension=extension,
        status="ringing",
        initiated_at=now,
        call_sid=f"sip-{call_id[:12]}",
    )
    db.add(new_call)
    await db.commit()
    await db.refresh(new_call)

    logger.info(
        f"[OUTBOUND CALL INITIATED] call_id={call_id}, tenant_id={tenant_id}, "
        f"from_ext={extension}, to={destination_number}"
    )

    # Push real-time event to tenant channel
    await dispatch_telephony_event(
        event="call_ringing",
        call_id=call_id,
        tenant_id=tenant_id,
        user_id=current_user.id,
        agent_id=payload.agent_id,
        status="ringing",
        extra_data={
            "caller_number": extension,
            "destination_number": destination_number,
            "direction": "outbound",
        },
    )

    return serialize_call(new_call)


# ---------------------------------------------------------------------------
# 3. Inbound Call Webhook / Trigger
# ---------------------------------------------------------------------------

@router.post("/calls/inbound", status_code=status.HTTP_201_CREATED)
async def handle_inbound_call(
    payload: TelephonyInboundRequest,
    db: AsyncSession = Depends(get_db),
):
    """
    Handles incoming SIP call event from Asterisk/FreePBX PBX trigger.
    Identifies target user/extension and associated tenant.
    Creates call record and broadcasts 'incoming_call' real-time event.
    """
    caller_number = validate_phone_number(payload.caller_number)
    extension = payload.destination_extension or "1001"
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Look up user or agent associated with extension
    user_stmt = select(User).where(User.sip_extension == extension, User.is_active == True)
    matched_user = (await db.execute(user_stmt)).scalars().first()

    # Determine tenant ID
    target_tenant_id = (
        payload.tenant_id
        or (matched_user.business_id if matched_user and matched_user.business_id else None)
    )

    if not target_tenant_id:
        # Fallback to primary business
        from app.core.deps import PRIMARY_BUSINESS_ID
        target_tenant_id = PRIMARY_BUSINESS_ID

    call_id = str(uuid.uuid4())
    user_id = matched_user.id if matched_user else None
    agent_id = payload.agent_id or user_id

    new_call = Call(
        id=call_id,
        business_id=target_tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        direction="inbound",
        from_number=caller_number,
        to_number=extension,
        sip_extension=extension,
        status="ringing",
        initiated_at=now,
        call_sid=f"sip-in-{call_id[:12]}",
    )
    db.add(new_call)
    await db.commit()
    await db.refresh(new_call)

    logger.info(
        f"[INBOUND CALL ARRIVED] call_id={call_id}, tenant_id={target_tenant_id}, "
        f"caller={caller_number}, extension={extension}, user_id={user_id}"
    )

    # Push real-time event to frontend via WebSocket
    await dispatch_telephony_event(
        event="incoming_call",
        call_id=call_id,
        tenant_id=target_tenant_id,
        user_id=user_id,
        agent_id=agent_id,
        status="ringing",
        extra_data={
            "caller_number": caller_number,
            "destination_number": extension,
            "extension": extension,
            "direction": "inbound",
        },
    )

    return serialize_call(new_call)


# ---------------------------------------------------------------------------
# 4. Call Control APIs (Accept, Reject, Hangup, Mute, Unmute)
# ---------------------------------------------------------------------------

async def _get_call_with_tenant_isolation(call_id: str, tenant_id: str, db: AsyncSession) -> Call:
    """Enforces strict tenant isolation. Rejects access if call belongs to another tenant."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call '{call_id}' not found.",
        )
    if call.business_id != tenant_id:
        logger.warning(
            f"[SECURITY ALERT] Unauthorized call control attempt: "
            f"authenticated_tenant={tenant_id} attempted to access call={call_id} "
            f"belonging to tenant={call.business_id}"
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Call belongs to a different tenant.",
        )
    return call


@router.post("/calls/{call_id}/accept")
async def accept_call(
    call_id: str,
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Accepts an incoming/ringing call and sets status to connected."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    call.status = "connected"
    call.connected_at = now
    if not call.user_id:
        call.user_id = current_user.id
    db.add(call)
    await db.commit()
    await db.refresh(call)

    logger.info(f"[CALL ANSWERED] call_id={call_id}, tenant_id={tenant_id}, user_id={current_user.id}")

    # If this was a live incoming SIP PBX call, answer on the SIP socket
    try:
        from app.services.sip_inbound_daemon import answer_incoming_sip_call
        await answer_incoming_sip_call(call.id)
    except Exception:
        pass

    await dispatch_telephony_event(
        event="call_answered",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status="answered",
    )
    await dispatch_telephony_event(
        event="call_connected",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status="connected",
    )

    return serialize_call(call)


@router.post("/calls/{call_id}/reject")
async def reject_call(
    call_id: str,
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Rejects an incoming/ringing call and sets status to rejected."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # If this was a live incoming SIP PBX call, decline on the SIP socket
    try:
        from app.services.sip_inbound_daemon import decline_incoming_sip_call
        await decline_incoming_sip_call(call.id)
    except Exception:
        pass

    call.status = "rejected"
    call.ended_at = now
    call.hangup_reason = "REJECTED"
    db.add(call)
    await db.commit()
    await db.refresh(call)

    logger.info(f"[CALL REJECTED] call_id={call_id}, tenant_id={tenant_id}, user_id={current_user.id}")

    await dispatch_telephony_event(
        event="call_rejected",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status="rejected",
    )

    return serialize_call(call)


@router.post("/calls/{call_id}/hangup")
async def hangup_call(
    call_id: str,
    current_user: User = Depends(get_current_db_user),
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Terminates an active call and records duration."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    call.status = "completed"
    call.ended_at = now
    call.hangup_reason = call.hangup_reason or "NORMAL_CLEARING"

    if call.connected_at:
        call.duration_seconds = max(0, int((now - call.connected_at).total_seconds()))
    elif call.initiated_at:
        call.duration_seconds = max(0, int((now - call.initiated_at).total_seconds()))

    try:
        from app.services.voice_service import hangup_sip_call
        hangup_sip_call(call.id)
    except Exception:
        pass

    db.add(call)
    await db.commit()
    await db.refresh(call)

    logger.info(
        f"[CALL ENDED] call_id={call_id}, tenant_id={tenant_id}, "
        f"duration={call.duration_seconds}s, reason={call.hangup_reason}"
    )

    await dispatch_telephony_event(
        event="call_ended",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status="completed",
        extra_data={"duration": call.duration_seconds, "hangup_reason": call.hangup_reason},
    )

    return serialize_call(call)


@router.post("/calls/{call_id}/mute")
async def mute_call(
    call_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Notifies participants/browser that audio input is muted."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)

    await dispatch_telephony_event(
        event="call_status_update",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status=call.status,
        extra_data={"is_muted": True},
    )

    return {"status": "muted", "call_id": call_id}


@router.post("/calls/{call_id}/unmute")
async def unmute_call(
    call_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Notifies participants/browser that audio input is unmuted."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)

    await dispatch_telephony_event(
        event="call_status_update",
        call_id=call.id,
        tenant_id=tenant_id,
        user_id=call.user_id,
        agent_id=call.agent_id,
        status=call.status,
        extra_data={"is_muted": False},
    )

    return {"status": "unmuted", "call_id": call_id}


@router.get("/calls/{call_id}", response_model=CallResponse)
async def get_call_details(
    call_id: str,
    tenant_id: str = Depends(get_current_tenant_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns details for a single call, enforcing tenant boundaries."""
    call = await _get_call_with_tenant_isolation(call_id, tenant_id, db)
    return CallResponse(**serialize_call(call))
