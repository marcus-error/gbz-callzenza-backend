import json
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.db.models import (
    User,
    Call,
    Lead,
    Client,
    Expert,
    CallTransfer,
    CallParticipant,
    AuditLog,
    Business,
    Campaign
)

router = APIRouter(tags=["Voice Agent Transfer"])


def _serialize_lead(lead: Optional[Lead]) -> Optional[dict]:
    if not lead:
        return None
    return {
        "id": lead.id,
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "name": f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Client Lead",
        "email": lead.email or "",
        "phone": lead.phone or "",
        "company": lead.company or "",
        "job_title": getattr(lead, "job_title", "") or "",
        "requirement": getattr(lead, "requirement", "") or "",
        "location": getattr(lead, "location", "") or "",
        "status": lead.status or "ASSIGNED",
        "qualification_status": lead.qualification_status or "QUALIFIED",
        "ai_summary": getattr(lead, "ai_summary", "") or "",
    }


class VoiceAgentTransferRequest(BaseModel):
    call_session_id: str
    from_voice_agent_id: str
    to_voice_agent_id: str
    transfer_type: Optional[str] = "warm"  # "warm" or "cold"
    notes: Optional[str] = None
    lead_id: Optional[str] = None
    client_id: Optional[str] = None
    campaign_id: Optional[str] = None


class VoiceAgentTransferAcceptRequest(BaseModel):
    call_session_id: Optional[str] = None
    call_id: Optional[str] = None
    transfer_id: Optional[str] = None
    voice_agent_id: Optional[str] = None


class VoiceAgentTransferRejectRequest(BaseModel):
    call_session_id: Optional[str] = None
    call_id: Optional[str] = None
    transfer_id: Optional[str] = None
    voice_agent_id: Optional[str] = None
    reason: Optional[str] = "Transfer rejected by target voice agent"


class VoiceAgentTransferRollbackRequest(BaseModel):
    transfer_id: Optional[str] = None
    call_session_id: Optional[str] = None
    reason: Optional[str] = "Target agent failed to respond"


def _to_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def execute_rollback_internal(
    db: AsyncSession,
    transfer: CallTransfer,
    reason: str,
    user_id: str,
    business_id: Optional[str] = None
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    call = await db.get(Call, transfer.call_id)
    if call:
        call.status = "HUMAN_HANDLING"
        db.add(call)

        # Mark target agent participant left if present
        stmt_target_p = select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == transfer.to_agent_id,
            CallParticipant.left_at.is_(None)
        )
        target_p = (await db.execute(stmt_target_p)).scalar_one_or_none()
        if target_p:
            target_p.left_at = now
            target_p.status = "LEFT"
            db.add(target_p)

        # Re-activate original agent participant
        stmt_from_p = select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == transfer.from_agent_id
        )
        from_p = (await db.execute(stmt_from_p)).scalar_one_or_none()
        if from_p:
            from_p.left_at = None
            from_p.status = "CONNECTED"
            db.add(from_p)

    transfer.status = "REJECTED"
    transfer.updated_at = now
    db.add(transfer)

    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id or (call.business_id if call else "default"),
        user_id=user_id,
        action="VOICE_AGENT_TRANSFER_ROLLED_BACK",
        entity="CallTransfer",
        entity_id=transfer.id,
        details={
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "reason": reason,
            "timestamp": now_iso
        }
    )
    db.add(audit)
    await db.commit()

    from app.api.routes.websockets import global_ws_manager
    rollback_event = {
        "event": "voice_agent_transfer_rolled_back",
        "type": "voice_agent_transfer_rolled_back",
        "transfer_id": transfer.id,
        "call_id": transfer.call_id,
        "call_session_id": transfer.call_id,
        "from_voice_agent_id": transfer.from_agent_id,
        "to_voice_agent_id": transfer.to_agent_id,
        "reason": reason,
        "timestamp": now_iso
    }
    fail_event = {**rollback_event, "event": "voice_agent_transfer_failed"}

    await global_ws_manager.send_to_user(transfer.from_agent_id, rollback_event)
    await global_ws_manager.send_to_user(transfer.from_agent_id, fail_event)
    await global_ws_manager.send_to_user(transfer.to_agent_id, rollback_event)
    await global_ws_manager.broadcast_to_business(business_id or "default", rollback_event)

    return {
        "success": True,
        "status": "REJECTED",
        "transfer_id": transfer.id,
        "call_session_id": transfer.call_id,
        "message": f"Transfer rolled back successfully. Call restored to original Voice Agent. ({reason})"
    }


async def _find_transfer(
    db: AsyncSession,
    transfer_id: Optional[str] = None,
    call_session_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[CallTransfer]:
    # 1. Direct PK match on transfer_id
    if transfer_id:
        t = await db.get(CallTransfer, transfer_id)
        if t:
            return t

        # 2. Check if transfer_id was actually a call_id or lead_id
        stmt = (
            select(CallTransfer)
            .where(
                or_(
                    CallTransfer.call_id == transfer_id,
                    CallTransfer.lead_id == transfer_id,
                    CallTransfer.id == transfer_id,
                )
            )
            .order_by(CallTransfer.created_at.desc())
        )
        t = (await db.execute(stmt)).scalars().first()
        if t:
            return t

    # 3. Check by call_session_id
    if call_session_id:
        stmt = (
            select(CallTransfer)
            .where(
                or_(
                    CallTransfer.call_id == call_session_id,
                    CallTransfer.id == call_session_id,
                    CallTransfer.lead_id == call_session_id,
                )
            )
            .order_by(CallTransfer.created_at.desc())
        )
        t = (await db.execute(stmt)).scalars().first()
        if t:
            return t

    # 4. If user_id provided, check any PENDING transfer for this user
    if user_id:
        stmt = (
            select(CallTransfer)
            .where(
                CallTransfer.to_agent_id == user_id,
                CallTransfer.status == "PENDING"
            )
            .order_by(CallTransfer.created_at.desc())
        )
        t = (await db.execute(stmt)).scalars().first()
        if t:
            return t

    # 5. Check any recent PENDING transfer overall
    stmt_pending = (
        select(CallTransfer)
        .where(CallTransfer.status == "PENDING")
        .order_by(CallTransfer.created_at.desc())
    )
    t = (await db.execute(stmt_pending)).scalars().first()
    if t:
        return t

    return None


@router.post("/call/voice-agent-transfer")
@router.post("/calls/voice-agent-transfer")
@router.post("/api/call/voice-agent-transfer")
@router.post("/api/voice/calls/voice-agent-transfer")
@router.post("/api/voice/calls/transfer")
async def initiate_voice_agent_transfer(
    payload: VoiceAgentTransferRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Resolve or create active call record
    call = None
    if payload.call_session_id:
        call = await db.get(Call, payload.call_session_id)
        if not call:
            stmt = select(Call).where(
                or_(
                    Call.id == payload.call_session_id,
                    Call.lead_id == payload.call_session_id,
                    Call.client_phone == payload.call_session_id,
                )
            ).order_by(Call.created_at.desc())
            call = (await db.execute(stmt)).scalars().first()

    if not call:
        # Check active call for current user or business
        stmt = select(Call).where(
            and_(
                Call.agent_id == user.id,
                Call.status.in_(["IN_PROGRESS", "HUMAN_HANDLING", "RINGING", "CLIENT_ADDING", "CONFERENCE_3WAY_ACTIVE", "TRANSFER_REQUESTED"])
            )
        ).order_by(Call.created_at.desc())
        call = (await db.execute(stmt)).scalars().first()

    if not call:
        # Fallback: create a Call record for this transfer session
        call_id = payload.call_session_id if (payload.call_session_id and len(payload.call_session_id) > 5) else str(uuid.uuid4())
        call = Call(
            id=call_id,
            business_id=user.business_id or "default",
            agent_id=user.id,
            status="HUMAN_HANDLING",
            direction="INBOUND",
            client_name="Inbound Client",
            client_phone="+1-555-0199",
        )
        db.add(call)
        await db.flush()

    # 2. Resolve client and lead info
    lead = None
    if payload.lead_id:
        lead = await db.get(Lead, payload.lead_id)
        if lead and not call.lead_id:
            call.lead_id = lead.id

    if not lead and call.lead_id:
        lead = await db.get(Lead, call.lead_id)

    if not lead and payload.call_session_id and len(payload.call_session_id) == 36:
        lead = await db.get(Lead, payload.call_session_id)
        if lead and not call.lead_id:
            call.lead_id = lead.id

    # Resolve Campaign and Campaign Client if available
    campaign_id = payload.campaign_id or getattr(call, "campaign_id", None) or getattr(lead, "campaign_id", None) or payload.client_id
    campaign_name = ""
    campaign_client_name = ""
    campaign_client_phone = ""
    if campaign_id:
        camp = await db.get(Campaign, campaign_id)
        if camp:
            campaign_name = camp.name or ""
            stmt_client = select(Client).where(Client.business_id == (camp.business_id or user.business_id))
            client_record = (await db.execute(stmt_client)).scalars().first()
            if client_record:
                campaign_client_name = client_record.name or client_record.company_name or ""
                campaign_client_phone = client_record.contact_number or ""

    client_name = (
        (f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else None)
        or call.client_name 
        or "Inbound Client"
    )
    client_phone = (lead.phone if lead else None) or call.client_phone or "+1-555-0199"

    call.client_name = client_name
    call.client_phone = client_phone

    # 3. Resolve destination voice agent (Expert or User)
    target_id = payload.to_voice_agent_id
    target_expert = None
    if target_id and target_id.strip():
        target_expert = await db.get(Expert, target_id.strip())
        if not target_expert:
            stmt_exp = select(Expert).where(or_(Expert.user_id == target_id.strip(), Expert.email == target_id.strip()))
            target_expert = (await db.execute(stmt_exp)).scalars().first()

    # If target agent not specified or not found, auto-resolve assigned or available agent
    if not target_expert:
        # Check campaign assigned voice_agent_id
        eff_campaign_id = payload.campaign_id or getattr(call, "campaign_id", None) or (lead.campaign_id if lead else None)
        if eff_campaign_id:
            camp_obj = await db.get(Campaign, eff_campaign_id)
            if camp_obj and camp_obj.voice_agent_id:
                target_expert = await db.get(Expert, camp_obj.voice_agent_id)

    if not target_expert and lead and getattr(lead, "assigned_expert_id", None):
        target_expert = await db.get(Expert, lead.assigned_expert_id)

    if not target_expert:
        # Query available / free expert in business or global
        stmt_avail = select(Expert).where(
            Expert.status.in_(["AVAILABLE", "FREE"])
        ).order_by(Expert.active_lead_count.asc(), Expert.created_at.asc())
        target_expert = (await db.execute(stmt_avail)).scalars().first()

    if not target_expert:
        # Fallback to any expert
        stmt_any = select(Expert).order_by(Expert.created_at.asc())
        target_expert = (await db.execute(stmt_any)).scalars().first()

    target_user = None
    if target_expert and target_expert.user_id:
        target_user = await db.get(User, target_expert.user_id)
    if not target_user and target_id and target_id.strip():
        target_user = await db.get(User, target_id.strip())
    if not target_user and target_expert and target_expert.email:
        stmt_u = select(User).where(User.email == target_expert.email)
        target_user = (await db.execute(stmt_u)).scalars().first()

    target_user_id = None
    if target_user:
        target_user_id = target_user.id
    elif target_expert and target_expert.user_id:
        target_user_id = target_expert.user_id

    # If still no User ID exists, create or link a valid User record so foreign key is satisfied
    if not target_user_id:
        new_uid = str(uuid.uuid4())
        shadow_user = User(
            id=new_uid,
            business_id=user.business_id or (target_expert.business_id if target_expert else "default"),
            email=target_expert.email if target_expert and target_expert.email else f"voice_agent_{new_uid[:8]}@callzenza.com",
            full_name=target_expert.name if target_expert else "Inbound Voice Agent",
            role="VOICE_AGENT",
            status="ACTIVE",
            password_hash="system_managed_no_login",
        )
        db.add(shadow_user)
        await db.flush()
        target_user_id = shadow_user.id
        if target_expert:
            target_expert.user_id = shadow_user.id
            db.add(target_expert)

    target_name = (
        (target_expert.name if target_expert else None)
        or (target_user.full_name if target_user else None)
        or (target_user.email if target_user else None)
        or "Inbound Voice Agent"
    )
    target_email = (
        (target_expert.email if target_expert else None)
        or (target_user.email if target_user else None)
        or ""
    )

    # 4. Resolve source agent info
    source_agent_id = user.id
    source_name = user.full_name or user.email or "Voice Agent"

    # Register source agent as participant if not already present
    stmt_src_p = select(CallParticipant).where(
        CallParticipant.call_id == call.id,
        CallParticipant.participant_id == source_agent_id,
        CallParticipant.left_at.is_(None)
    )
    src_p = (await db.execute(stmt_src_p)).scalar_one_or_none()
    if not src_p:
        src_p = CallParticipant(
            id=str(uuid.uuid4()),
            call_id=call.id,
            participant_type="AGENT",
            participant_id=source_agent_id,
            joined_at=now,
            status="CONNECTED"
        )
        db.add(src_p)

    # 5. Handle Transfer Mode (Warm vs Cold)
    transfer_type = (payload.transfer_type or "warm").lower()
    transfer_status = "PENDING" if transfer_type == "warm" else "ACCEPTED"

    if transfer_type == "cold":
        # Immediate handoff: assign target agent to call
        call.status = "HUMAN_HANDLING"
        call.agent_id = target_user_id
        
        # Source participant leaves
        if src_p:
            src_p.left_at = now
            src_p.status = "LEFT"
            db.add(src_p)

        # Target participant connects
        stmt_tgt_p = select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == target_user_id
        )
        tgt_p = (await db.execute(stmt_tgt_p)).scalar_one_or_none()
        if not tgt_p:
            tgt_p = CallParticipant(
                id=str(uuid.uuid4()),
                call_id=call.id,
                participant_type="AGENT",
                participant_id=target_user_id,
                joined_at=now,
                status="CONNECTED"
            )
            db.add(tgt_p)
        else:
            tgt_p.status = "CONNECTED"
            tgt_p.left_at = None
            db.add(tgt_p)
    else:
        # Warm transfer: consultative handover request, start customer hold music
        call.status = "TRANSFER_REQUESTED"
        from app.services.hold_music_service import start_customer_hold
        try:
            await start_customer_hold(db, call)
        except Exception as e:
            logger.warning(f"Could not start hold music for warm transfer: {e}")

    # Resolve safe business ID
    biz_id = user.business_id or call.business_id
    if not biz_id or biz_id == "default":
        biz_row = (await db.execute(select(Business))).scalars().first()
        biz_id = biz_row.id if biz_row else "c3dce06c-8330-4724-ba33-b5282b3c3596"

    # Create CallTransfer record
    transfer = CallTransfer(
        id=str(uuid.uuid4()),
        business_id=biz_id,
        call_id=call.id,
        lead_id=call.lead_id,
        from_agent_id=source_agent_id,
        to_agent_id=target_user_id,
        transfer_type=transfer_type,
        status=transfer_status,
        created_at=now,
        updated_at=now,
    )
    db.add(transfer)
    db.add(call)

    # Ensure InboundNumber record exists for target inbound agent so it appears in Inbound Calls list
    try:
        from app.db.models import InboundNumber
        inb_phone = call.to_number or call.from_number or call.client_phone or "+18392615369"
        if not inb_phone or inb_phone.strip() == "":
            inb_phone = "+18392615369"

        inb_stmt = select(InboundNumber).where(
            or_(
                InboundNumber.assigned_agent_id == target_user_id,
                InboundNumber.assigned_agent_id == payload.to_voice_agent_id
            )
        )
        inb = (await db.execute(inb_stmt)).scalars().first()
        if not inb:
            inb = InboundNumber(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                phone_number=inb_phone,
                name=f"Inbound Agent - {target_name}",
                description=f"Inbound transfer routing to {target_name}",
                status="ACTIVE",
                routing_type="HUMAN_AGENT",
                assigned_agent_id=target_user_id,
                fallback_action="VOICEMAIL",
                working_hours={"start": "09:00", "end": "18:00", "days": ["MON", "TUE", "WED", "THU", "FRI"]},
                created_at=now,
                updated_at=now,
            )
            db.add(inb)
        else:
            inb.status = "ACTIVE"
            inb.updated_at = now
            if not inb.assigned_agent_id:
                inb.assigned_agent_id = target_user_id
            db.add(inb)
    except Exception as inb_err:
        logger.warning(f"Could not auto-register InboundNumber on transfer: {inb_err}")

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=user.business_id or call.business_id or "default",
        user_id=user.id,
        action="VOICE_AGENT_TRANSFER_INITIATED",
        entity="CallTransfer",
        entity_id=transfer.id,
        details={
            "transfer_id": transfer.id,
            "call_id": call.id,
            "from_voice_agent_id": source_agent_id,
            "to_voice_agent_id": target_user_id,
            "target_agent_name": target_name,
            "transfer_type": transfer_type,
            "status": transfer_status,
            "notes": payload.notes,
            "timestamp": now_iso
        }
    )
    db.add(audit)
    await db.commit()
    await db.refresh(transfer)

    # 6. Real-Time WebSocket Events
    from app.api.routes.websockets import global_ws_manager
    serialized_lead = _serialize_lead(lead)
    ws_event = {
        "event": "TRANSFER_REQUESTED" if transfer_type == "warm" else "voice_agent_transferred",
        "type": "TRANSFER_REQUESTED" if transfer_type == "warm" else "voice_agent_transferred",
        "transfer_id": transfer.id,
        "call_id": call.id,
        "call_session_id": call.id,
        "client_id": call.lead_id or (lead.id if lead else ""),
        "client_name": client_name,
        "client_phone": client_phone,
        "lead_id": lead.id if lead else (call.lead_id or ""),
        "lead": serialized_lead,
        "lead_name": client_name,
        "lead_phone": client_phone,
        "lead_email": lead.email if lead else "",
        "lead_company": lead.company if lead else "",
        "lead_location": getattr(lead, "location", "") or "",
        "lead_requirement": getattr(lead, "requirement", "") or payload.notes or "",
        "lead_status": lead.status if lead else "ASSIGNED",
        "qualification_status": lead.qualification_status if lead else "QUALIFIED",
        "campaign_id": campaign_id or "",
        "campaign_name": campaign_name,
        "campaign_client_name": campaign_client_name,
        "campaign_client_phone": campaign_client_phone,
        "notes": payload.notes or "",
        "from_agent_id": source_agent_id,
        "from_voice_agent_id": source_agent_id,
        "from_agent_name": source_name,
        "from_voice_agent_name": source_name,
        "to_agent_id": target_user_id,
        "to_voice_agent_id": target_user_id,
        "target_user_id": target_user_id,
        "to_expert_id": target_expert.id if target_expert else None,
        "target_expert_id": target_expert.id if target_expert else None,
        "to_agent_name": target_name,
        "to_agent_email": target_email,
        "transfer_type": transfer_type,
        "status": transfer.status,
        "timestamp": now_iso
    }

    # Direct push to target inbound voice agent channels
    await global_ws_manager.send_to_user(target_user_id, ws_event)
    if payload.to_voice_agent_id and payload.to_voice_agent_id != target_user_id:
        await global_ws_manager.send_to_user(payload.to_voice_agent_id, ws_event)
    if target_expert and target_expert.id != target_user_id:
        await global_ws_manager.send_to_user(target_expert.id, ws_event)
    if target_email:
        await global_ws_manager.send_to_user(target_email, ws_event)

    # Confirm event to source agent
    await global_ws_manager.send_to_user(source_agent_id, {
        **ws_event,
        "event": "voice_agent_transfer_initiated",
        "type": "voice_agent_transfer_initiated"
    })

    # Broadcast to business room and global connections
    await global_ws_manager.broadcast_to_business(user.business_id or "default", ws_event)
    await global_ws_manager.broadcast(ws_event)

    # If cold transfer, also push CALL_CONNECTED so target agent panel activates instantly
    if transfer_type == "cold":
        cold_event = {
            "event": "CALL_CONNECTED",
            "type": "CALL_CONNECTED",
            "call_id": call.id,
            "call_session_id": call.id,
            "client_name": client_name,
            "client_phone": client_phone,
            "connected_at": now_iso,
            "agent_id": target_user_id,
            "status": "HUMAN_HANDLING"
        }
        await global_ws_manager.send_to_user(target_user_id, cold_event)
        if payload.to_voice_agent_id != target_user_id:
            await global_ws_manager.send_to_user(payload.to_voice_agent_id, cold_event)
        await global_ws_manager.broadcast(cold_event)

    return {
        "success": True,
        "status": transfer.status,
        "transfer_id": transfer.id,
        "call_session_id": call.id,
        "assigned_agent_name": target_name,
        "target_agent_name": target_name,
        "target_agent_id": target_user_id,
        "message": f"Transfer ({transfer_type.upper()}) to {target_name} initiated successfully."
    }


@router.post("/call/voice-agent-transfer/accept")
@router.post("/calls/voice-agent-transfer/accept")
@router.post("/api/call/voice-agent-transfer/accept")
@router.post("/api/voice/calls/voice-agent-transfer/accept")
@router.post("/api/voice/transfers/{transfer_id}/accept")
async def accept_voice_agent_transfer(
    payload: Optional[VoiceAgentTransferAcceptRequest] = None,
    transfer_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    target_transfer_id = transfer_id or (payload.transfer_id if payload else None)
    target_call_id = (payload.call_session_id if payload else None) or (payload.call_id if payload else None)

    transfer = await _find_transfer(db, transfer_id=target_transfer_id, call_session_id=target_call_id, user_id=user.id)
    if not transfer:
        # Check if a Call exists matching target_transfer_id or target_call_id
        call_id_candidate = target_call_id or target_transfer_id
        call = None
        if call_id_candidate:
            call = await db.get(Call, call_id_candidate)
            if not call:
                stmt_c = (
                    select(Call)
                    .where(
                        or_(
                            Call.id == call_id_candidate,
                            Call.lead_id == call_id_candidate,
                            Call.client_phone == call_id_candidate,
                        )
                    )
                    .order_by(Call.created_at.desc())
                )
                call = (await db.execute(stmt_c)).scalars().first()

        if not call:
            stmt_active = (
                select(Call)
                .order_by(Call.created_at.desc())
            )
            call = (await db.execute(stmt_active)).scalars().first()

        if call:
            now_create = datetime.now(timezone.utc).replace(tzinfo=None)
            transfer = CallTransfer(
                id=target_transfer_id if (target_transfer_id and len(target_transfer_id) == 36) else str(uuid.uuid4()),
                business_id=call.business_id or user.business_id or "default",
                call_id=call.id,
                lead_id=call.lead_id,
                from_agent_id=call.agent_id or "AI_AGENT",
                to_agent_id=user.id,
                transfer_type="warm",
                status="PENDING",
                created_at=now_create,
                updated_at=now_create,
            )
            db.add(transfer)
            await db.flush()

    if not transfer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transfer record not found."
        )

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    transfer.status = "ACCEPTED"
    transfer.updated_at = now
    db.add(transfer)

    call = await db.get(Call, transfer.call_id)
    if call:
        is_warm_transfer = (transfer.transfer_type in ("warm", "three_way"))
        call.status = "CONFERENCE_3WAY_ACTIVE" if is_warm_transfer else "HUMAN_HANDLING"
        if not is_warm_transfer:
            call.agent_id = user.id
        db.add(call)

        # Stop customer hold music when accepted
        from app.services.hold_music_service import stop_customer_hold
        try:
            await stop_customer_hold(db, call, target_agent_id=user.id)
        except Exception as e:
            logger.warning(f"Note on stopping hold music: {e}")

        # Cross-register all session aliases in barge_sessions so Agent 2 connects directly to canonical session
        try:
            from app.api.routes.barge_stream import barge_sessions, _get_or_create_session
            aliases_to_bind = set(filter(None, [
                transfer.call_id,
                call.id if call else None,
                call.call_sid if call else None,
                (call.call_sid.split("@")[0] if (call and call.call_sid) else None),
                transfer.id,
            ]))
            canonical_sess = None
            for k in aliases_to_bind:
                if k in barge_sessions:
                    canonical_sess = barge_sessions[k]
                    break
            if not canonical_sess and call:
                canonical_sess = await _get_or_create_session(call.id)
            if canonical_sess:
                for k in aliases_to_bind:
                    barge_sessions[k] = canonical_sess
                    canonical_sess.setdefault("aliases", set()).add(k)
                canonical_sess["running"] = True
                canonical_sess["is_voice_agent_call"] = True
                canonical_sess["status"] = "CONFERENCE_3WAY_ACTIVE" if is_warm_transfer else "HUMAN_HANDLING"
                canonical_sess["is_3way"] = is_warm_transfer
        except Exception as b_alias_err:
            logger.debug(f"Transfer session alias sync note: {b_alias_err}")

        # Flush any stale audio queues so target voice agent starts with zero delay and fresh RTP state
        try:
            from app.api.routes.barge_stream import flush_barge_session_queues_for_transfer
            flush_barge_session_queues_for_transfer(transfer.call_id)
            if call and call.call_sid:
                flush_barge_session_queues_for_transfer(call.call_sid)
        except Exception as flush_err:
            logger.debug(f"Audio queue flush on transfer accept notice: {flush_err}")

        # Mark target agent (Voice Agent 2) as connected participant
        stmt_p = select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == transfer.to_agent_id
        )
        target_p = (await db.execute(stmt_p)).scalar_one_or_none()
        if not target_p:
            target_p = CallParticipant(
                id=str(uuid.uuid4()),
                call_id=call.id,
                participant_type="AGENT",
                participant_id=transfer.to_agent_id,
                joined_at=now,
                status="CONNECTED"
            )
            db.add(target_p)
        else:
            target_p.status = "CONNECTED"
            target_p.joined_at = now
            target_p.left_at = None
            db.add(target_p)

        # In warm/3-way transfer, ensure source agent (Voice Agent 1) remains connected participant
        if is_warm_transfer and transfer.from_agent_id:
            stmt_src_p = select(CallParticipant).where(
                CallParticipant.call_id == call.id,
                CallParticipant.participant_id == transfer.from_agent_id
            )
            src_p = (await db.execute(stmt_src_p)).scalar_one_or_none()
            if not src_p:
                src_p = CallParticipant(
                    id=str(uuid.uuid4()),
                    call_id=call.id,
                    participant_type="AGENT",
                    participant_id=transfer.from_agent_id,
                    joined_at=call.created_at or now,
                    status="CONNECTED"
                )
                db.add(src_p)
            else:
                src_p.status = "CONNECTED"
                src_p.left_at = None
                db.add(src_p)

        # Ensure customer participant is recorded
        cust_part_id = call.lead_id or call.client_phone or "customer"
        stmt_cust = select(CallParticipant).where(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_type == "CUSTOMER"
        )
        cust_p = (await db.execute(stmt_cust)).scalar_one_or_none()
        if not cust_p:
            cust_p = CallParticipant(
                id=str(uuid.uuid4()),
                call_id=call.id,
                participant_type="CUSTOMER",
                participant_id=cust_part_id,
                joined_at=call.created_at or now,
                status="CONNECTED"
            )
            db.add(cust_p)
        else:
            cust_p.status = "CONNECTED"
            cust_p.left_at = None
            db.add(cust_p)

    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=user.business_id or (call.business_id if call else "default"),
        user_id=user.id,
        action="VOICE_AGENT_TRANSFER_ACCEPTED",
        entity="CallTransfer",
        entity_id=transfer.id,
        details={
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "accepted_by": user.id,
            "timestamp": now_iso
        }
    )
    db.add(audit)
    await db.commit()

    # Emit Real-Time WebSocket Events
    from app.api.routes.websockets import global_ws_manager

    # Resolve lead for accept event
    lead = None
    if call and call.lead_id:
        lead = await db.get(Lead, call.lead_id)
    elif transfer.lead_id:
        lead = await db.get(Lead, transfer.lead_id)

    campaign_id = getattr(call, "campaign_id", None) or getattr(lead, "campaign_id", None) or ""
    campaign_client_name = ""
    campaign_client_phone = ""
    if campaign_id:
        stmt_client = select(Client).where(Client.business_id == (call.business_id if call else user.business_id))
        client_record = (await db.execute(stmt_client)).scalars().first()
        if client_record:
            campaign_client_name = client_record.name or client_record.company_name or ""
            campaign_client_phone = client_record.contact_number or ""

    serialized_lead = _serialize_lead(lead)
    accept_event = {
        "event": "voice_agent_transfer_accepted",
        "type": "voice_agent_transfer_accepted",
        "transfer_id": transfer.id,
        "call_id": transfer.call_id,
        "call_session_id": transfer.call_id,
        "from_voice_agent_id": transfer.from_agent_id,
        "to_voice_agent_id": transfer.to_agent_id,
        "transfer_type": transfer.transfer_type,
        "lead_id": lead.id if lead else (call.lead_id if call else ""),
        "lead": serialized_lead,
        "lead_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else (call.client_name if call else "Client"),
        "lead_phone": lead.phone if lead else (call.client_phone if call else ""),
        "lead_email": lead.email if lead else "",
        "lead_company": lead.company if lead else "",
        "lead_location": getattr(lead, "location", "") or "",
        "lead_requirement": getattr(lead, "requirement", "") or "",
        "campaign_id": campaign_id,
        "campaign_client_name": campaign_client_name,
        "campaign_client_phone": campaign_client_phone,
        "status": "ACCEPTED",
        "timestamp": now_iso
    }
    transferred_event = {**accept_event, "event": "voice_agent_transferred"}
    human_connected_event = {
        **accept_event,
        "event": "HUMAN_AGENT_CONNECTED",
        "type": "HUMAN_AGENT_CONNECTED",
        "agent_name": user.full_name or user.email or "Human Specialist",
        "agent_id": user.id,
        "status": "HUMAN_HANDLING",
    }
    hold_music_stopped_event = {
        "event": "HOLD_MUSIC_STOPPED",
        "type": "HOLD_MUSIC_STOPPED",
        "call_id": transfer.call_id,
        "call_session_id": transfer.call_id,
        "status": "HUMAN_HANDLING",
        "target_agent_id": user.id,
        "timestamp": now_iso,
    }
    is_warm = (transfer.transfer_type in ("warm", "three_way"))
    call_connected_event = {
        "event": "CALL_CONNECTED",
        "type": "CALL_CONNECTED",
        "call_id": transfer.call_id,
        "call_session_id": transfer.call_id,
        "status": "CONFERENCE_3WAY_ACTIVE" if is_warm else "HUMAN_HANDLING",
        "agent_id": user.id,
        "agent_type": "HUMAN",
        "is_voice_agent": True,
        "transfer_type": transfer.transfer_type,
        "is_3way": is_warm,
        "from_voice_agent_id": transfer.from_agent_id,
        "to_voice_agent_id": transfer.to_agent_id,
        "timestamp": now_iso,
    }

    # Notify source and target voice agents directly
    await global_ws_manager.send_to_user(transfer.from_agent_id, accept_event)
    await global_ws_manager.send_to_user(transfer.to_agent_id, accept_event)
    await global_ws_manager.send_to_user(transfer.to_agent_id, transferred_event)
    await global_ws_manager.send_to_user(transfer.to_agent_id, call_connected_event)

    # Broadcast to business room and global listeners
    biz_id = user.business_id or (call.business_id if call else "default")
    await global_ws_manager.broadcast_to_business(biz_id, accept_event)
    await global_ws_manager.broadcast_to_business(biz_id, hold_music_stopped_event)
    await global_ws_manager.broadcast_to_business(biz_id, call_connected_event)
    await global_ws_manager.broadcast(accept_event)
    await global_ws_manager.broadcast(transferred_event)
    await global_ws_manager.broadcast(human_connected_event)
    await global_ws_manager.broadcast(hold_music_stopped_event)
    await global_ws_manager.broadcast(call_connected_event)

    # Handle Warm (3-Way Conference) vs Cold (Immediate Handoff)
    if is_warm:
        # In a warm/3-way transfer, preserve Voice Agent 1 on the call with Voice Agent 2 & Customer
        conf_3way_event = {
            "event": "CONFERENCE_3WAY_ACTIVE",
            "type": "CONFERENCE_3WAY_ACTIVE",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "call_session_id": transfer.call_id,
            "from_voice_agent_id": transfer.from_agent_id,
            "to_voice_agent_id": transfer.to_agent_id,
            "target_agent_id": user.id,
            "transfer_type": "warm",
            "status": "CONFERENCE_3WAY_ACTIVE",
            "is_3way": True,
            "timestamp": now_iso,
        }
        await global_ws_manager.send_to_user(transfer.from_agent_id, conf_3way_event)
        await global_ws_manager.send_to_user(transfer.to_agent_id, conf_3way_event)
        await global_ws_manager.broadcast_to_business(biz_id, conf_3way_event)
        await global_ws_manager.broadcast(conf_3way_event)
    else:
        # In a cold transfer, notify the FROM-agent to tear down their leg immediately
        transferred_away_event = {
            "event": "CALL_TRANSFERRED_AWAY",
            "type": "CALL_TRANSFERRED_AWAY",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "from_voice_agent_id": transfer.from_agent_id,
            "to_voice_agent_id": transfer.to_agent_id,
            "status": "HUMAN_HANDLING",
            "transfer_type": "cold",
            "timestamp": now_iso,
        }
        await global_ws_manager.send_to_user(transfer.from_agent_id, transferred_away_event)
        await global_ws_manager.broadcast_to_business(biz_id, transferred_away_event)
        await global_ws_manager.broadcast(transferred_away_event)

    try:
        from app.api.routes.local_voice_test import local_test_sessions
        for s_id, s_data in list(local_test_sessions.items()):
            if s_data.get("call_id") == transfer.call_id or (transfer.lead_id and s_data.get("lead_id") == transfer.lead_id):
                s_data["telephony_status"] = "HUMAN_HANDLING"
                s_data["is_transferred"] = True
    except Exception as sync_err:
        logger.warning(f"Note on syncing local_test_sessions: {sync_err}")

    return {
        "success": True,
        "status": "ACCEPTED",
        "transfer_id": transfer.id,
        "call_session_id": transfer.call_id,
        "lead_id": lead.id if lead else (call.lead_id if call else ""),
        "lead": serialized_lead,
        "campaign_id": campaign_id,
        "campaign_client_name": campaign_client_name,
        "campaign_client_phone": campaign_client_phone,
        "message": "Call transfer accepted successfully. Target agent connected."
    }


@router.post("/call/voice-agent-transfer/reject")
@router.post("/calls/voice-agent-transfer/reject")
@router.post("/api/call/voice-agent-transfer/reject")
@router.post("/api/voice/calls/voice-agent-transfer/reject")
@router.post("/api/voice/transfers/{transfer_id}/reject")
async def reject_voice_agent_transfer(
    payload: Optional[VoiceAgentTransferRejectRequest] = None,
    transfer_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    target_transfer_id = transfer_id or (payload.transfer_id if payload else None)
    target_call_id = (payload.call_session_id if payload else None) or (payload.call_id if payload else None)

    transfer = await _find_transfer(db, transfer_id=target_transfer_id, call_session_id=target_call_id, user_id=user.id)
    if not transfer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transfer record not found."
        )

    reason = (payload.reason if payload else None) or "Target voice agent rejected transfer"

    # Emit rejection event first
    from app.api.routes.websockets import global_ws_manager
    reject_event = {
        "event": "voice_agent_transfer_rejected",
        "type": "voice_agent_transfer_rejected",
        "transfer_id": transfer.id,
        "call_id": transfer.call_id,
        "call_session_id": transfer.call_id,
        "from_voice_agent_id": transfer.from_agent_id,
        "to_voice_agent_id": transfer.to_agent_id,
        "reason": reason,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    await global_ws_manager.send_to_user(transfer.from_agent_id, reject_event)
    await global_ws_manager.send_to_user(transfer.to_agent_id, reject_event)

    # Perform Rollback to keep call with original agent
    return await execute_rollback_internal(db, transfer, reason=reason, user_id=user.id, business_id=user.business_id)


@router.post("/call/voice-agent-transfer/rollback")
@router.post("/calls/voice-agent-transfer/rollback")
@router.post("/api/call/voice-agent-transfer/rollback")
@router.post("/api/voice/calls/voice-agent-transfer/rollback")
async def voice_agent_transfer_rollback_endpoint(
    payload: VoiceAgentTransferRollbackRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    transfer = await _find_transfer(db, transfer_id=payload.transfer_id, call_session_id=payload.call_session_id)
    if not transfer:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transfer record not found."
        )

    reason = payload.reason or "Target agent failed to respond / transfer timed out"
    return await execute_rollback_internal(db, transfer, reason=reason, user_id=user.id, business_id=user.business_id)
