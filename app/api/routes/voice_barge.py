import logging
import json
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.database import get_db
from app.db.models import Call, User, AuditLog, Client
from app.core.deps import get_current_db_user
from app.core.config import get_settings

router = APIRouter(prefix="/api/voice/calls", tags=["voice-barge"])

_logger = logging.getLogger(__name__)
settings = get_settings()

ACTIVE_CALL_STATUSES = (
    "IN_PROGRESS", "RINGING", "HUMAN_HANDLING", "AI_HANDLING",
    "CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING", "TRANSFER_REQUESTED", "QUEUED",
    "QUALIFIED", "PARKED", "CALL_PARKED", "AGENT_ASSIGNED", "ON_HOLD", "TALKING",
    "ACTIVE", "CONNECTED", "LIVE", "CALLING", "BRIDGED", "ANSWERED",
    "DIALING", "IN_CALL", "RESERVED",
    "initiating", "ringing", "answered", "in_progress"
)

def _validate_supervisor_role(user: User):
    role = (user.role or "").strip().upper()
    if role not in ("ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER", "AGENT", "VOICE_AGENT"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to monitor or barge into live calls."
        )


async def _load_and_validate_call(call_id: str, user: User, db: AsyncSession) -> Call:
    _validate_supervisor_role(user)

    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Call {call_id} not found."
        )

    call_status = (call.status or "").strip().upper()
    if call_status not in ACTIVE_CALL_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Call {call_id} is no longer active (status: {call.status})."
        )

    return call


@router.get("/active", status_code=status.HTTP_200_OK)
async def get_active_calls(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns list of currently active calls for the live call console.
    Supports 1:1 through 1:100 calls without unbounded rendering.
    """
    _validate_supervisor_role(user)
    from app.db.models import Campaign, Lead

    stmt = (
        select(Call, Campaign.name.label("campaign_name"), Lead)
        .outerjoin(Campaign, Call.campaign_id == Campaign.id)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .where(
            Call.business_id == user.business_id,
            Call.status.in_(ACTIVE_CALL_STATUSES)
        )
        .order_by(Call.created_at.desc())
        .limit(100)
    )
    results = (await db.execute(stmt)).all()

    active_calls = []
    for call, campaign_name, lead in results:
        lead_display_name = ""
        if lead:
            lead_display_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or lead.phone or ""
        if not lead_display_name:
            lead_display_name = call.client_name or call.to_number or "Customer"

        last_msg = "Call in progress"
        if call.transcript:
            lines = [l.strip() for l in call.transcript.splitlines() if l.strip()]
            if lines:
                last_msg = lines[-1]

        active_calls.append({
            "call_id": call.id,
            "campaign_id": call.campaign_id,
            "campaign_name": campaign_name or "Direct Call",
            "lead_name": lead_display_name,
            "agent_id": call.agent_id,
            "status": call.status,
            "duration_seconds": call.duration_seconds or 0,
            "last_message": last_msg,
            "direction": call.direction or "OUTBOUND",
        })
    return active_calls


def _ensure_twilio_stream(call: Call):
    """
    Ensures Twilio media stream is attached to the live call for real-time audio.
    """
    if not call.call_sid:
        return

    if settings.twilio_account_sid and settings.twilio_auth_token and settings.public_base_url:
        try:
            from twilio.rest import Client as TwilioRestClient
            client = TwilioRestClient(settings.twilio_account_sid, settings.twilio_auth_token)
            ws_host = settings.public_base_url.replace("http://", "ws://").replace("https://", "wss://")
            customer_stream_url = f"{ws_host}/api/voice/barge/stream/customer/{call.id}"

            # Attach media stream capturing both tracks
            client.calls(call.call_sid).streams.create(
                url=customer_stream_url,
                track="both_tracks"
            )
            _logger.info(f"Attached Twilio media stream to call {call.id} (SID: {call.call_sid})")
        except Exception as e:
            _logger.warning(f"Could not attach Twilio media stream (might already be streaming): {e}")


@router.post("/{call_id}/listen", status_code=status.HTTP_200_OK)
async def start_listen(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Establishes a Listen-only monitoring session for an active call.
    """
    call = await _load_and_validate_call(call_id, user, db)

    # Attach live stream from telephony provider if available
    _ensure_twilio_stream(call)

    # Record AuditLog
    audit = AuditLog(
        user_id=user.id,
        business_id=user.business_id,
        action="LISTEN_STARTED",
        entity="Call",
        entity_id=call.id,
        details={"user_email": user.email, "call_sid": call.call_sid, "mode": "LISTEN"}
    )
    db.add(audit)
    await db.commit()

    # Broadcast event
    from app.api.routes.websockets import global_ws_manager
    await global_ws_manager.broadcast(json.dumps({
        "event": "SUPERVISOR_LISTENING",
        "call_id": call.id,
        "supervisor_id": user.id,
        "supervisor_name": user.full_name or user.email,
        "message": f"Supervisor {user.full_name or user.email} is listening."
    }))

    return {
        "ok": True,
        "mode": "LISTEN",
        "status": "LISTENING",
        "call_id": call.id,
        "stream_endpoint": f"/api/voice/barge/stream/browser/listen/{call.id}",
        "message": "Listen session initialized."
    }


@router.post("/{call_id}/stop-listen", status_code=status.HTTP_200_OK)
async def stop_listen(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Stops a Listen-only monitoring session without affecting the active call.
    """
    _validate_supervisor_role(user)

    call = await db.get(Call, call_id)
    if call:
        audit = AuditLog(
            user_id=user.id,
            business_id=user.business_id,
            action="LISTEN_STOPPED",
            entity="Call",
            entity_id=call.id,
            details={"user_email": user.email, "mode": "LISTEN"}
        )
        db.add(audit)
        await db.commit()

        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast(json.dumps({
            "event": "SUPERVISOR_STOPPED_LISTENING",
            "call_id": call.id,
            "supervisor_id": user.id
        }))

    return {"ok": True, "message": "Listen session closed."}


@router.post("/{call_id}/barge", status_code=status.HTTP_200_OK)
async def start_barge(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Establishes a two-way Barge-In session for an active call.
    """
    call = await _load_and_validate_call(call_id, user, db)

    # Attach live stream from telephony provider
    _ensure_twilio_stream(call)

    # Record AuditLog
    audit = AuditLog(
        user_id=user.id,
        business_id=user.business_id,
        action="BARGE_IN_STARTED",
        entity="Call",
        entity_id=call.id,
        details={"user_email": user.email, "call_sid": call.call_sid, "mode": "BARGE_IN"}
    )
    db.add(audit)
    await db.commit()

    from app.api.routes.websockets import global_ws_manager
    await global_ws_manager.broadcast(json.dumps({
        "event": "ADMIN_BARGED_IN",
        "call_id": call.id,
        "admin_id": user.id,
        "admin_name": user.full_name or user.email,
        "message": f"{user.full_name or 'Admin'} joined the live call."
    }))

    return {
        "ok": True,
        "mode": "BARGE_IN",
        "status": "BARGED_IN",
        "call_id": call.id,
        "stream_endpoint": f"/api/voice/barge/stream/browser/barge/{call.id}",
        "message": "Barge-in bridge initialized."
    }


@router.post("/{call_id}/leave-barge", status_code=status.HTTP_200_OK)
async def end_barge(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Leaves a Barge-In session while keeping the active customer-agent call alive.
    """
    _validate_supervisor_role(user)

    call = await db.get(Call, call_id)
    if call:
        audit = AuditLog(
            user_id=user.id,
            business_id=user.business_id,
            action="BARGE_IN_STOPPED",
            entity="Call",
            entity_id=call.id,
            details={"user_email": user.email, "mode": "BARGE_IN"}
        )
        db.add(audit)
        await db.commit()

        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast(json.dumps({
            "event": "ADMIN_LEFT_BARGE",
            "call_id": call.id,
            "admin_id": user.id
        }))

    try:
        from app.api.routes.barge_stream import disconnect_browser_bargers
        await disconnect_browser_bargers(call_id, target_user_id=user.id)
    except Exception as e:
        _logger.warning(f"Error disconnecting barge stream for {call_id}: {e}")

    return {"ok": True, "message": "Left barge session successfully."}
