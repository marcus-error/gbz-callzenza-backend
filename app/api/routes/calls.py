import json
import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import get_current_db_user, get_optional_db_user
from app.core.timezone import to_utc_iso
from app.db.database import get_db
from app.db.models import AIAgent, AuditLog, Business, Call, Campaign, Conversation, Lead, User
from app.services.voice_service import get_voice_provider, is_mock_mode

router = APIRouter(prefix="/api/calls", tags=["calls"])
settings = get_settings()

ACTIVE_CALL_STATUSES = (
    "initiating",
    "ringing",
    "answered",
    "in_progress",
    "HUMAN_HANDLING",
    "AI_HANDLING",
    "CONFERENCE_3WAY_ACTIVE",
    "CLIENT_ADDING",
    "TRANSFER_REQUESTED",
    "QUEUED",
    "RINGING",
    "IN_PROGRESS",
    "CONNECTED",
    "CALLING",
    "TALKING",
    "ON_CALL",
    "DIALING",
    "IN_CALL",
    "RESERVED",
    "ACTIVE",
    "LIVE",
    "BRIDGED",
    "dialing",
    "reserved",
)



class OutboundCallRequest(BaseModel):
    phone_number: Optional[str] = Field(None, description="Destination phone number for manual dial")
    lead_id: Optional[str] = Field(None, description="Database Lead ID to look up phone number")
    campaign_id: Optional[str] = Field(None, description="Campaign ID to associate with the call")
    agent_id: Optional[str] = Field(None, description="AI or Voice Agent ID")
    agent_type: Optional[str] = Field(None, description="'AI_VOICE' or 'HUMAN'")
    sip_account_id: Optional[str] = Field(None, description="Explicit SIP account database ID to use for call")


class EndCallRequest(BaseModel):
    duration_seconds: Optional[int] = 0
    outcome: Optional[str] = "COMPLETED"
    qualification_status: Optional[str] = None
    only_ai_leg: Optional[bool] = False


def normalize_to_e164(phone: str) -> str:
    """Normalizes phone number to standard E.164 format (+<country_code><number>).
    Rejects invalid characters and malformed structures.
    """
    if not phone or not isinstance(phone, str):
        return ""
    raw = phone.strip()
    if raw.startswith("new-"):
        raw = raw[4:].strip()

    digits = "".join(c for c in raw if c.isdigit())
    if not digits:
        return ""

    # Strip international dial prefix if present
    if digits.startswith("0091"):
        digits = digits[4:]
    elif digits.startswith("01191"):
        digits = digits[5:]

    if raw.startswith("+") and 7 <= len(digits) <= 15:
        clean = f"+{digits}"
    elif digits.startswith("00") and len(digits) > 2:
        clean = f"+{digits[2:]}"
    elif digits.startswith("91") and len(digits) == 12:
        clean = f"+{digits}"
    elif digits.startswith("1") and len(digits) == 11:
        clean = f"+{digits}"
    elif digits.startswith("0") and len(digits) == 11 and digits[1] in "6789":
        clean = f"+91{digits[1:]}"
    elif len(digits) == 10 and digits[0] in "6789":
        clean = f"+91{digits}"
    elif len(digits) > 10 and digits[-10] in "6789":
        clean = f"+91{digits[-10:]}"
    elif len(digits) == 10:
        clean = f"+91{digits}"
    else:
        clean = f"+{digits}"

    # E.164 standard: + followed by 7 to 15 digits
    if not re.match(r"^\+[1-9]\d{6,14}$", clean):
        return f"+{digits}" if digits else ""
    return clean


@router.post("/outbound", status_code=status.HTTP_201_CREATED)
async def initiate_outbound_call(
    payload: OutboundCallRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Initiates an actual outbound call to either:
    1. A manual dialed phone number (FLOW 1)
    2. A Lead from the database via lead_id (FLOW 2)
    Connects through Twilio PSTN / SIP provider to ring the destination phone.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    logger.info(f"[CALL API] outbound request received lead_id={payload.lead_id} campaign_id={payload.campaign_id} phone_number={payload.phone_number}")
    logger.info(
        f"[OUTBOUND_CALL_REQUEST] user_id={user.id} email={user.email} role={user.role} "
        f"lead_id={payload.lead_id} campaign_id={payload.campaign_id} phone_number={payload.phone_number}"
    )

    # 1. Active Call Protection: Check if Voice Agent already has an active call
    active_stmt = select(Call).where(
        Call.agent_id == user.id,
        Call.status.in_(ACTIVE_CALL_STATUSES),
        Call.ended_at.is_(None),
    )
    existing_call = (await db.execute(active_stmt)).scalars().first()
    if existing_call:
        logger.warning(f"[OUTBOUND_CALL_REJECTED] Active call already exists for user={user.id}: call_id={existing_call.id}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active call already exists. Please complete or hang up the current call before placing a new one.",
        )

    # 2. Determine Destination Phone & Lead context
    lead: Optional[Lead] = None
    destination_raw: str = ""
    target_lead_id: Optional[str] = payload.lead_id

    if target_lead_id:
        logger.info(f"[LEAD_LOOKUP] lead_id={target_lead_id}")
        lead = await db.get(Lead, target_lead_id)
        if not lead:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")

        # 3. Tenant / Business Validation for Lead
        if user.business_id and lead.business_id != user.business_id:
            logger.warning(
                f"[SECURITY_VIOLATION] Cross-tenant lead call attempt! user_biz={user.business_id}, lead_biz={lead.business_id}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access or dial leads belonging to another tenant.",
            )

        if not lead.phone or not str(lead.phone).strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Lead does not have a valid phone number",
            )
        destination_raw = str(lead.phone).strip()
    elif payload.phone_number:
        destination_raw = str(payload.phone_number).strip()
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either 'phone_number' (for manual dial) or 'lead_id' (for lead dial) must be provided.",
        )

    # 4. Normalize destination phone number to E.164
    normalized_dest = normalize_to_e164(destination_raw)
    if not normalized_dest:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid phone number format: '{destination_raw}'. Must be a valid E.164 phone number (e.g. +918608949822).",
        )
    logger.info(f"[DESTINATION_RESOLVED] raw='{destination_raw}' -> normalized='{normalized_dest}'")

    # 4b. Strict DND / Do Not Call suppression check
    from app.services.dnd_service import is_number_suppressed
    target_biz_id = user.business_id or (lead.business_id if lead else None)
    try:
        if await is_number_suppressed(db, normalized_dest, target_biz_id):
            logger.warning(f"[DND_SUPPRESSED] Number '{normalized_dest}' is on DND suppression list. Outbound call rejected.")
            if lead:
                lead.call_status = "DNC"
                lead.status = "DNC"
                lead.disposition = "DNC"
                db.add(lead)
                await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Number '{normalized_dest}' is on the Do Not Call (DND) suppression list. Outbound call prohibited.",
            )
    except HTTPException:
        raise
    except Exception as dnd_err:
        logger.warning(f"[DND_CHECK_ERROR] Error checking suppression status for '{normalized_dest}': {dnd_err}")

    # 5. Validate Campaign if supplied
    campaign_id: Optional[str] = None
    campaign_obj: Optional[Campaign] = None
    if payload.campaign_id:
        campaign_obj = await db.get(Campaign, payload.campaign_id)
        if not campaign_obj:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
        if user.business_id and campaign_obj.business_id != user.business_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to access campaigns belonging to another tenant.",
            )
        campaign_id = campaign_obj.id
        if lead:
            from app.db.models import CampaignLead
            c_lead_stmt = select(CampaignLead).where(
                CampaignLead.campaign_id == campaign_obj.id,
                CampaignLead.lead_id == lead.id
            )
            c_lead = (await db.execute(c_lead_stmt)).scalars().first()
            if not c_lead:
                in_lead_group = False
                if getattr(campaign_obj, "lead_group_id", None) and getattr(lead, "lead_group_id", None):
                    if lead.lead_group_id == campaign_obj.lead_group_id:
                        in_lead_group = True
                if not in_lead_group:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Lead '{lead.id}' is not assigned to campaign '{campaign_obj.name}'. Cross-campaign dialing is prohibited."
                    )

    # 5b. Resolve Agent: AI Agent or Voice Agent
    ai_agent = None
    camp_primary_type = "NONE"
    transfer_agent_ids = "NONE"
    decision_reason = ""

    # Priority 1: Explicit human or AI override in payload
    p_type = str(payload.agent_type).strip().upper() if payload.agent_type else None
    if p_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT"):
        effective_agent_type = "HUMAN"
        effective_agent_id = payload.agent_id or (campaign_obj.voice_agent_id if campaign_obj else None) or user.id
        decision_reason = "payload_explicit_human"
    elif p_type in ("AI", "AI_VOICE", "AI_AGENT"):
        effective_agent_type = "AI_VOICE"
        decision_reason = "payload_explicit_ai"
        if payload.agent_id:
            ai_agent = await db.get(AIAgent, payload.agent_id)
        if not ai_agent and campaign_obj and campaign_obj.agent_id:
            ai_agent = await db.get(AIAgent, campaign_obj.agent_id)
        if not ai_agent:
            ai_agent = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
        effective_agent_id = ai_agent.id if ai_agent else user.id
    elif campaign_obj:
        camp_primary_type = (campaign_obj.agent_type or "AI_VOICE").upper()
        transfer_agent_ids = campaign_obj.voice_agent_id or "NONE"
        if (
            camp_primary_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
            or "HUMAN" in camp_primary_type
        ):
            effective_agent_type = "HUMAN"
            effective_agent_id = campaign_obj.voice_agent_id or user.id
            decision_reason = "campaign_configured_as_human"
        else:
            # AI Agent Campaign: Primary is AI Agent! Backup human Voice Agents are strictly transfer backup!
            effective_agent_type = "AI_VOICE"
            decision_reason = "campaign_configured_as_ai"
            if campaign_obj.agent_id:
                ai_agent = await db.get(AIAgent, campaign_obj.agent_id)
            if not ai_agent and payload.agent_id:
                ai_agent = await db.get(AIAgent, payload.agent_id)
            if not ai_agent:
                ai_agent = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
            if not ai_agent:
                ai_agent = (await db.execute(select(AIAgent).order_by(AIAgent.created_at.asc()))).scalars().first()
            effective_agent_id = ai_agent.id if ai_agent else user.id
    else:
        # Non-campaign / manual calls:
        if payload.agent_id:
            ai_agent = await db.get(AIAgent, payload.agent_id)
        if not ai_agent and (payload.agent_type in ("AI_VOICE", "AI", "AI_AGENT") or user.role == "AI_AGENT"):
            ai_agent = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
        if not ai_agent and not payload.agent_type and user.role not in ("VOICE_AGENT", "AGENT"):
            ai_agent = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()

        is_human_assigned = (
            (payload.agent_type and str(payload.agent_type).upper() in ("HUMAN", "VOICE", "VOICE_AGENT"))
            or (not ai_agent and user.role in ("VOICE_AGENT", "AGENT", "ADMIN", "SUPER_ADMIN", "SALES_EXPERT"))
        )

        if is_human_assigned and not (payload.agent_type and str(payload.agent_type).upper() in ("AI", "AI_VOICE")):
            effective_agent_type = "HUMAN"
            effective_agent_id = payload.agent_id or user.id
            decision_reason = "manual_call_assigned_human"
        else:
            effective_agent_type = "AI_VOICE"
            effective_agent_id = ai_agent.id if ai_agent else user.id
            decision_reason = "manual_call_assigned_ai"

    # 6. Resolve Business ID & Caller ID
    biz_id = user.business_id or (lead.business_id if lead else "") or (ai_agent.business_id if ai_agent else "")
    if not biz_id:
        biz_row = (await db.execute(select(Business))).scalars().first()
        biz_id = biz_row.id if biz_row else ""

    # Outbound Caller ID selection: Campaign twilio number -> Settings twilio number -> fallback
    caller_id = (
        (campaign_obj.twilio_number if campaign_obj and campaign_obj.twilio_number else None)
        or settings.twilio_phone_number
        or ""
    )

    # 6b. Resolve or create Conversation
    conversation = Conversation(
        business_id=biz_id,
        lead_id=lead.id if lead else None,
        agent_id=effective_agent_id,
        channel="VOICE",
        status="ACTIVE",
    )
    db.add(conversation)
    await db.flush()

    # 7. Create Call Record in DB with status = 'initiating'
    call_id = f"call-{uuid.uuid4().hex[:12]}"
    call = Call(
        id=call_id,
        business_id=biz_id,
        lead_id=lead.id if lead else None,
        campaign_id=campaign_id,
        conversation_id=conversation.id,
        agent_id=effective_agent_id,
        user_id=user.id,
        direction="OUTBOUND",
        agent_type=effective_agent_type,
        from_number=caller_id,
        to_number=normalized_dest,
        status="initiating",
        initiated_at=now,
        duration_seconds=0,
    )
    db.add(call)

    # Tag barge session for outbound call
    try:
        from app.api.routes.barge_stream import _get_or_create_session
        sess = await _get_or_create_session(call.id)
        sess["is_dashboard_outbound"] = True
        sess["agent_type"] = effective_agent_type
        sess["is_voice_agent_call"] = (effective_agent_type == "HUMAN")
    except Exception as exc:
        logger.debug(f"Barge session tag note: {exc}")

    # Mark user and lead status
    user.status = "ON CALL"
    db.add(user)
    if lead:
        lead.call_status = "RINGING"
        lead.status = "CALLING"
        lead.last_attempt_at = now
        lead.attempt_count = (lead.attempt_count or 0) + 1
        db.add(lead)

    await db.commit()
    await db.refresh(call)
    logger.info(f"[CALL_RECORD_CREATED] call_id={call.id} status={call.status} destination={normalized_dest}")
    logger.info(
        f"[CALL_ROUTING] call_id={call.id} direction=OUTBOUND "
        f"campaign_id={(campaign_id or 'NONE')} "
        f"campaign_primary_agent_type={camp_primary_type} "
        f"primary_agent_id={effective_agent_id} "
        f"transfer_agent_ids={transfer_agent_ids} "
        f"resolved_agent_type={effective_agent_type} "
        f"decision_reason='{decision_reason}' "
        f"ai_bot={'WILL_START' if effective_agent_type == 'AI_VOICE' else 'SKIPPED'}"
    )

    # 8. Call Telephony Provider (Twilio / SIP)
    assigned_sip_acc = None
    user_org_id = user.business_id or getattr(user, "organization_id", None)
    if getattr(payload, "sip_account_id", None):
        from app.services.sip_account_service import validate_sip_account_for_campaign
        is_val, err_m, assigned_sip_acc = await validate_sip_account_for_campaign(
            db, payload.sip_account_id, user_org_id, call_direction="outbound"
        )
        if not is_val:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Selected SIP account unavailable: {err_m}"
            )
        call.sip_account_id = assigned_sip_acc.id
        if assigned_sip_acc.caller_id:
            caller_id = assigned_sip_acc.caller_id
        db.add(call)
        await db.commit()
    elif campaign_id:
        camp_obj = await db.get(Campaign, campaign_id)
        if camp_obj:
            if not getattr(camp_obj, "sip_account_id", None):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Campaign '{camp_obj.name}' has no outbound SIP account assigned. Please assign an active SIP account before dialing."
                )
            from app.services.sip_account_service import validate_sip_account_for_campaign
            is_val, err_m, assigned_sip_acc = await validate_sip_account_for_campaign(
                db, camp_obj.sip_account_id, camp_obj.business_id, call_direction="outbound"
            )
            if not is_val:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Campaign SIP account unavailable: {err_m}"
                )
            call.sip_account_id = assigned_sip_acc.id
            if getattr(camp_obj, "caller_did_id", None):
                from app.db.models import DIDNumber
                did_row = await db.get(DIDNumber, camp_obj.caller_did_id)
                if did_row:
                    if did_row.organization_id and camp_obj.business_id and did_row.organization_id != camp_obj.business_id:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Tenant isolation violation: DID '{did_row.phone_number}' belongs to another organization."
                        )
                    if did_row.sip_account_id and did_row.sip_account_id != assigned_sip_acc.id:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Configured DID '{did_row.phone_number}' does not belong to the selected SIP account."
                        )
                    if hasattr(did_row, "status") and did_row.status and did_row.status.upper() not in ("ACTIVE", "CONFIGURED"):
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"Configured DID '{did_row.phone_number}' is inactive."
                        )
                    if did_row.phone_number:
                        caller_id = did_row.phone_number.strip()
            if not caller_id and assigned_sip_acc.caller_id:
                caller_id = assigned_sip_acc.caller_id.strip()
            if not caller_id and getattr(camp_obj, "twilio_number", None) and camp_obj.twilio_number != "LOCAL_TEST":
                caller_id = camp_obj.twilio_number.strip()
            if caller_id:
                call.from_number = caller_id
            db.add(call)
            await db.commit()

    # If no SIP account resolved yet, check organization default enabled SIP account (strictly for non-campaign calls)
    if not assigned_sip_acc and user_org_id and not campaign_id:
        from app.db.models import SIPAccount
        acc_stmt = select(SIPAccount).where(
            SIPAccount.organization_id == user_org_id,
            SIPAccount.enabled == True,
            func.lower(SIPAccount.direction).in_(["outbound", "both", "bidirectional"]),
        )
        org_accs = (await db.execute(acc_stmt)).scalars().all()
        default_acc = next((a for a in org_accs if getattr(a, "is_default", False) or (isinstance(getattr(a, "settings", None), dict) and a.settings.get("is_default"))), None)
        if not default_acc and len(org_accs) == 1:
            default_acc = org_accs[0]
        if default_acc:
            assigned_sip_acc = default_acc
            call.sip_account_id = assigned_sip_acc.id
            if assigned_sip_acc.caller_id:
                caller_id = assigned_sip_acc.caller_id
            db.add(call)
            await db.commit()

    if assigned_sip_acc:
        logger.info(
            f"[OUTBOUND_SIP_RESOLVED] call_id={call.id} sip_account_id={assigned_sip_acc.id} "
            f"account_name='{assigned_sip_acc.name}' host={assigned_sip_acc.host}:{assigned_sip_acc.port} "
            f"transport={assigned_sip_acc.transport} caller_id='{caller_id}'"
        )

    provider = get_voice_provider()
    from app.services.voice_service import SIPVoiceProvider
    if isinstance(provider, SIPVoiceProvider) and not assigned_sip_acc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No valid SIP configuration found for this campaign or organization. Please configure and assign an active SIP account in the Admin Dashboard."
        )

    provider_name = type(provider).__name__
    logger.info(f"[TELEPHONY_REQUEST] provider={provider_name} to={normalized_dest} from={caller_id} (sip_account={getattr(assigned_sip_acc, 'name', None)})")

    public_base = settings.public_base_url.rstrip("/")
    answer_url = f"{public_base}/api/voice/answer?call_id={call.id}"
    status_url = f"{public_base}/api/voice/status?call_id={call.id}"

    try:
        provider_call_id = await provider.place_call(
            to=normalized_dest,
            from_=caller_id,
            answer_url=answer_url,
            status_callback_url=status_url,
            sip_account=assigned_sip_acc,
        )
        logger.info(f"[PROVIDER_RESPONSE] provider_call_id={provider_call_id}")

        # Update Call record: status -> ringing, save provider_call_id (call_sid)
        call.call_sid = provider_call_id
        call.status = "ringing"
        if lead:
            lead.twilio_call_sid = provider_call_id
            db.add(lead)
        db.add(call)
        await db.commit()
        await db.refresh(call)

        # Broadcast via WebSockets for live UI synchrony
        try:
            from app.api.routes.websockets import global_ws_manager

            lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else "Customer"
            await global_ws_manager.broadcast(
                json.dumps({
                    "event": "CALL_CONNECTED",
                    "call_id": call.id,
                    "lead_id": call.lead_id,
                    "lead_name": lead_name,
                    "status": "ringing",
                    "destination": normalized_dest,
                    "provider_call_id": provider_call_id,
                    "initiated_at": to_utc_iso(now),
                })
            )
        except Exception as ws_err:
            logger.debug(f"WS notification notice: {ws_err}")

        return {
            "success": True,
            "call_id": call.id,
            "provider_call_id": provider_call_id,
            "status": call.status,
            "destination": normalized_dest,
            "caller_id": caller_id,
            "lead_id": call.lead_id,
            "campaign_id": call.campaign_id,
            "initiated_at": to_utc_iso(now),
            "provider": provider_name,
        }

    except Exception as exc:
        logger.error(f"[TELEPHONY_ERROR] Failed to place call via {provider_name}: {exc}")
        call.status = "failed"
        call.call_result = str(exc)[:255]
        call.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
        user.status = "AVAILABLE"
        if lead:
            lead.call_status = "FAILED"
            db.add(lead)
        db.add(call)
        db.add(user)
        await db.commit()

        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Telephony provider rejected the call: {str(exc)}",
        )


@router.get("/{call_id}")
async def get_call_status(
    call_id: str,
    user: Optional[User] = Depends(get_optional_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Retrieves real-time status and telemetry of a call."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")

    if user and user.business_id and call.business_id != user.business_id and user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    from app.db.models import CallMessage
    msg_stmt = select(CallMessage).where(CallMessage.call_id == call.id).order_by(CallMessage.timestamp.asc())
    db_msgs = (await db.scalars(msg_stmt)).all()
    messages_list = [
        {
            "id": m.id,
            "sender": "user" if (m.sender_type or "").upper() == "CUSTOMER" else "ai",
            "text": m.content,
            "timestamp": to_utc_iso(m.timestamp) if m.timestamp else None,
        }
        for m in db_msgs
    ]

    return {
        "call_id": call.id,
        "provider_call_id": call.call_sid,
        "status": call.status,
        "direction": call.direction,
        "destination": call.to_number,
        "caller_id": call.from_number,
        "lead_id": call.lead_id,
        "campaign_id": call.campaign_id,
        "agent_id": call.agent_id,
        "duration_seconds": call.duration_seconds or 0,
        "recording_url": call.recording_url or "",
        "transcript": call.transcript or "",
        "turn_count": call.turn_count or 0,
        "messages": messages_list,
        "initiated_at": to_utc_iso(call.initiated_at) if call.initiated_at else None,
        "connected_at": to_utc_iso(call.connected_at) if call.connected_at else None,
        "ended_at": to_utc_iso(call.ended_at) if call.ended_at else None,
    }


@router.post("/{call_id}/end")
async def end_call(
    call_id: str,
    payload: Optional[EndCallRequest] = None,
    user: Optional[User] = Depends(get_optional_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Terminates the actual live provider call and marks status completed in DB."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Call not found")

    if user and user.business_id and call.business_id != user.business_id and user.role != "SUPER_ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")

    # Guard: If call was transferred to human voice agent and only AI leg cleanup was requested,
    # do NOT terminate the customer leg.
    is_with_human = call.status in ("WITH_HUMAN_AGENT", "HUMAN_HANDLING")
    if is_with_human and payload and payload.only_ai_leg:
        logger.info(
            f"[CALL_END_GUARD] Call {call_id} is actively WITH_HUMAN_AGENT. "
            f"Preserving customer leg {call.call_sid}; only AI leg is disconnected."
        )
        return {"status": call.status, "message": "AI leg disconnected. Customer call maintained with Voice Agent."}

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Hang up on SIP provider (Asterisk)
    try:
        from app.services.voice_service import hangup_sip_call
        hangup_sip_call(call_id)
        if call.call_sid:
            hangup_sip_call(call.call_sid)
    except Exception as sip_err:
        logger.warning(f"SIP hangup notice: {sip_err}")

    # 2. Hang up on Twilio / provider if active call_sid is present
    if call.call_sid and settings.twilio_account_sid and settings.twilio_auth_token:
        try:
            import asyncio
            from twilio.rest import Client

            client = Client(settings.twilio_account_sid, settings.twilio_auth_token)

            def _terminate():
                try:
                    return client.calls(call.call_sid).update(status="completed")
                except Exception as t_err:
                    logger.warning(f"Twilio call termination notice for {call.call_sid}: {t_err}")
                    return None

            await asyncio.to_thread(_terminate)
            logger.info(f"[PROVIDER_HANGUP_SUCCESS] Terminated Twilio call leg for {call.call_sid}")
        except Exception as exc:
            logger.warning(f"Failed to hang up Twilio provider call: {exc}")

    # 3. Update DB status and duration
    call.status = "completed"
    call.ended_at = now
    if payload and payload.duration_seconds:
        call.duration_seconds = payload.duration_seconds
    elif call.connected_at:
        call.duration_seconds = max(0, int((now - call.connected_at).total_seconds()))

    if user:
        db_user = await db.get(User, user.id)
        if db_user:
            db_user.status = "AVAILABLE"
            db.add(db_user)


    if call.lead_id:
        lead = await db.get(Lead, call.lead_id)
        if lead:
            lead.call_status = "COMPLETED"
            if payload and payload.outcome:
                lead.call_result = payload.outcome
            if payload and payload.qualification_status:
                lead.qualification_status = payload.qualification_status
            db.add(lead)

    # Synchronize CampaignLead status and trigger refill
    if call.campaign_id:
        from app.db.models import CampaignLead
        from app.workers.tasks import trigger_campaign_refill
        cl_stmt = select(CampaignLead).where(
            CampaignLead.campaign_id == call.campaign_id,
            (CampaignLead.call_id == call.id) | (CampaignLead.lead_id == call.lead_id)
        )
        cl_res = await db.execute(cl_stmt)
        c_leads = cl_res.scalars().all()
        for c_lead in c_leads:
            c_lead.status = "COMPLETED"
            db.add(c_lead)

    db.add(call)
    await db.commit()
    await db.refresh(call)

    if call.campaign_id:
        logger.info(
            f"[{datetime.utcnow().isoformat()}] [CALL_SLOT_RELEASED] campaign_id={call.campaign_id} "
            f"call_id={call.id} lead_id={call.lead_id} reason='call_ended_endpoint'"
        )
        from app.workers.tasks import trigger_campaign_refill
        trigger_campaign_refill(call.campaign_id)


    # 3. Broadcast WebSocket event
    try:
        from app.api.routes.websockets import global_ws_manager

        await global_ws_manager.broadcast(
            json.dumps({
                "event": "CALL_COMPLETED",
                "call_id": call.id,
                "status": "completed",
                "duration_seconds": call.duration_seconds,
            })
        )
    except Exception as ws_err:
        logger.debug(f"WS notice: {ws_err}")

    return {
        "success": True,
        "call_id": call.id,
        "status": call.status,
        "duration_seconds": call.duration_seconds,
        "message": "Call successfully terminated.",
    }


@router.post("/cleanup-stale")
async def cleanup_stale_calls(
    stale_after_seconds: int = 300,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Force-completes all calls stuck in active statuses for the current user's business.
    Resets agent user.status to AVAILABLE.
    Useful after browser crashes, network drops, or any disconnect that skips end_call.
    Default threshold: calls active > 5 minutes (300s) are treated as stale.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = datetime.fromtimestamp(
        (now.timestamp() - stale_after_seconds), tz=None
    )

    # Find stale active calls for this business
    stmt = select(Call).where(
        Call.business_id == user.business_id,
        Call.status.in_(list(ACTIVE_CALL_STATUSES)),
        Call.ended_at.is_(None),
    )
    # Further narrow to ones that started before the cutoff
    result = (await db.execute(stmt)).scalars().all()

    stale = [
        c for c in result
        if (c.initiated_at or c.connected_at) and
           (now - (c.connected_at or c.initiated_at)).total_seconds() >= stale_after_seconds
    ]

    if not stale:
        # Also reset user status if stuck
        if user.status == "ON CALL":
            user.status = "AVAILABLE"
            db.add(user)
            await db.commit()
        return {"cleaned": 0, "message": "No stale calls found. Agent status reset if applicable."}

    cleaned_ids = []
    for stale_call in stale:
        stale_call.status = "completed"
        stale_call.ended_at = now
        if stale_call.connected_at:
            stale_call.duration_seconds = max(0, int((now - stale_call.connected_at).total_seconds()))
        db.add(stale_call)

        # Update lead status
        if stale_call.lead_id:
            lead = await db.get(Lead, stale_call.lead_id)
            if lead:
                lead.call_status = "COMPLETED"
                db.add(lead)

        if stale_call.campaign_id:
            from app.db.models import CampaignLead
            cl_stmt = select(CampaignLead).where(
                CampaignLead.campaign_id == stale_call.campaign_id,
                (CampaignLead.call_id == stale_call.id) | (CampaignLead.lead_id == stale_call.lead_id)
            )
            c_lead = (await db.execute(cl_stmt)).scalars().first()
            if c_lead:
                c_lead.status = "COMPLETED"
                db.add(c_lead)

        cleaned_ids.append(stale_call.id)

    # Reset the calling agent's status
    if stale:
        agent_ids = list({c.agent_id for c in stale if c.agent_id})
        for agent_id in agent_ids:
            agent = await db.get(User, agent_id)
            if agent and agent.status == "ON CALL":
                agent.status = "AVAILABLE"
                db.add(agent)

    # Also reset the requesting user
    if user:
        db_user = await db.get(User, user.id)
        if db_user and db_user.status == "ON CALL":
            db_user.status = "AVAILABLE"
            db.add(db_user)


    await db.commit()

    # Trigger refill for affected campaigns
    if stale:
        from app.workers.tasks import trigger_campaign_refill
        camp_ids = list({c.campaign_id for c in stale if c.campaign_id})
        for cid in camp_ids:
            trigger_campaign_refill(cid)


    logger.info(f"[STALE_CALL_CLEANUP] Completed {len(cleaned_ids)} stale calls: {cleaned_ids}")
    return {
        "cleaned": len(cleaned_ids),
        "call_ids": cleaned_ids,
        "message": f"Force-completed {len(cleaned_ids)} stale call(s) and reset agent status.",
    }
