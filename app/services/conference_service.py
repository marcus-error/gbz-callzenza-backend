"""
CallZenza — Multi-Agent Conference Calling Service
Enables multi-party real-time conference calling with:
- Customer (lead)
- Original Voice Agent (Host)
- Additional Voice Agent (Agent 2)
- Any further Voice Agents (Agent 3, Agent 4...) up to configured capacity
Provides full state tracking: INVITED, RINGING, CONNECTED, FAILED, LEFT.
Integrates with barge_sessions audio mixer and telephonic conference bridge without disconnecting existing parties.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from fastapi import HTTPException, status
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import (
    Call,
    CallParticipant,
    CallTransfer,
    Lead,
    Client,
    User,
    Expert,
    Business,
    AuditLog,
)
from app.api.routes.websockets import global_ws_manager

MAX_CONFERENCE_CAPACITY = 5


def _to_utc_iso(dt: Optional[datetime]) -> str:
    if not dt:
        return datetime.now(timezone.utc).isoformat()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def get_or_create_conference_room(
    call_id: str,
    db: AsyncSession,
    current_user: Optional[User] = None
) -> tuple[Call, str]:
    """
    Resolves Call record and ensures a unique conference_sid is assigned.
    Supports resolution by:
    - Direct Call.id
    - Clean SIP Call-ID (stripping '@' domain)
    - Stripped 'call-' prefix (e.g. 'call-99feea470eb6' -> '99feea470eb6')
    - Prefixed 'call-' ID
    - Active barge_sessions alias / db_call_id
    - ACTIVE_SIP_CALLS registry
    - CallTransfer.id or CallTransfer.call_id
    - Call.lead_id or Call.client_phone
    - Active live call fallback for current user/tenant
    """
    if not call_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="call_id must be provided."
        )

    call = None

    # 1. Direct primary key lookup
    try:
        call = await db.get(Call, call_id)
    except Exception:
        call = None

    clean_id = call_id.split("@")[0].strip() if "@" in call_id else call_id.strip()
    stripped_id = clean_id[5:] if clean_id.startswith("call-") else clean_id
    prefixed_id = f"call-{clean_id}" if not clean_id.startswith("call-") else clean_id

    candidate_ids = list(dict.fromkeys([c for c in [call_id, clean_id, stripped_id, prefixed_id] if c]))

    # 2. Try looking up through active in-memory barge_sessions
    if not call:
        try:
            from app.api.routes.barge_stream import barge_sessions
            for c_cand in candidate_ids:
                sess = barge_sessions.get(c_cand)
                if sess:
                    db_id = sess.get("db_call_id") or sess.get("call_id")
                    if db_id and db_id not in candidate_ids:
                        candidate_ids.append(db_id)
                        call = await db.get(Call, db_id)
                        if call:
                            break
            if not call:
                for sess in list(barge_sessions.values()):
                    sess_aliases = sess.get("aliases", set())
                    if any(c in sess_aliases for c in candidate_ids) or sess.get("db_call_id") in candidate_ids:
                        db_id = sess.get("db_call_id") or sess.get("call_id")
                        if db_id:
                            call = await db.get(Call, db_id)
                            if call:
                                break
        except Exception as e:
            logger.debug(f"[CONF_RESOLVE_BARGE] Note on barge_sessions lookup: {e}")

    # 3. Try looking up through active SIP calls registry
    if not call:
        try:
            from app.services.voice_service import ACTIVE_SIP_CALLS as VOICE_SIP_CALLS
            from app.services.sip_inbound_daemon import ACTIVE_SIP_CALLS as INBOUND_SIP_CALLS
            for sip_registry in [VOICE_SIP_CALLS, INBOUND_SIP_CALLS]:
                for c_cand in candidate_ids:
                    info = sip_registry.get(c_cand)
                    if info and info.get("db_call_id"):
                        db_id = info["db_call_id"]
                        call = await db.get(Call, db_id)
                        if call:
                            break
                if call:
                    break
        except Exception as e:
            logger.debug(f"[CONF_RESOLVE_SIP] Note on ACTIVE_SIP_CALLS lookup: {e}")

    # 4. Comprehensive SQL Query matching all standard identifiers & prefixes
    if not call:
        conditions = [
            Call.id.in_(candidate_ids),
            Call.call_sid.in_(candidate_ids),
            Call.lead_id.in_(candidate_ids),
            Call.client_phone.in_(candidate_ids),
            Call.conference_sid.in_(candidate_ids),
        ]
        # Match SIP call_sid with @domain or prefix:
        if clean_id:
            conditions.append(Call.call_sid.like(f"{clean_id}@%"))
            conditions.append(Call.call_sid.like(f"{clean_id}%"))
            conditions.append(Call.id.like(f"{clean_id}%"))
        if stripped_id and len(stripped_id) >= 6:
            conditions.append(Call.id.like(f"%{stripped_id}%"))
            conditions.append(Call.call_sid.like(f"%{stripped_id}%"))

        stmt = select(Call).where(or_(*conditions)).order_by(Call.created_at.desc())
        call = (await db.execute(stmt)).scalars().first()

    # 5. Check CallTransfer records (if call_id is a transfer ID)
    if not call:
        stmt_t = select(CallTransfer).where(
            or_(
                CallTransfer.id.in_(candidate_ids),
                CallTransfer.call_id.in_(candidate_ids),
            )
        ).order_by(CallTransfer.created_at.desc())
        transfer = (await db.execute(stmt_t)).scalars().first()
        if transfer and transfer.call_id:
            call = await db.get(Call, transfer.call_id)

    # 6. Fallback for "active-live-call" or general active call request
    if not call and (clean_id in ["active-live-call", "active", "live", "current"] or not call_id):
        conds = [
            Call.status.in_([
                "CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING", "IN_PROGRESS",
                "HUMAN_HANDLING", "AI_HANDLING", "CONNECTED", "initiating", "calling"
            ])
        ]
        if current_user and getattr(current_user, "business_id", None):
            conds.append(Call.business_id == current_user.business_id)
        if current_user and getattr(current_user, "role", "") not in ["SUPER_ADMIN", "ADMIN"]:
            conds.append(or_(Call.user_id == current_user.id, Call.agent_id == current_user.id))
        stmt_active = select(Call).where(and_(*conds)).order_by(Call.created_at.desc())
        call = (await db.execute(stmt_active)).scalars().first()

    if not call:
        logger.warning(
            f"[CONFERENCE_RESOLVE_FAIL] Active call record not found for call_id='{call_id}' "
            f"(candidate_ids={candidate_ids})."
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active call record not found for call_id='{call_id}'."
        )

    if not call.conference_sid:
        call.conference_sid = f"CONF-{uuid.uuid4().hex[:12].upper()}"
        db.add(call)
        await db.commit()
        await db.refresh(call)

    # Cross-register all resolved aliases in barge_sessions
    try:
        from app.api.routes.barge_stream import _get_or_create_session, barge_sessions
        session = await _get_or_create_session(call.id)
        aliases = session.setdefault("aliases", set())
        for a in [call.id, call.conference_sid, call.call_sid, call_id, clean_id, stripped_id, prefixed_id]:
            if a:
                barge_sessions[a] = session
                aliases.add(a)
        session["db_call_id"] = call.id
    except Exception as e:
        logger.debug(f"[CONF_SESSION_ALIAS] Note on barge_sessions registration: {e}")

    return call, call.conference_sid



async def list_conference_participants(call_id: str, db: AsyncSession) -> Dict[str, Any]:
    """
    Returns the real-time list of participants in a call conference:
    Customer, Original Host Agent, and all additional invited/connected Voice Agents.
    """
    call, conf_sid = await get_or_create_conference_room(call_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Resolve Lead info
    lead = None
    if call.lead_id:
        lead = await db.get(Lead, call.lead_id)

    # 2. Query CallParticipant records
    stmt_p = (
        select(CallParticipant)
        .where(CallParticipant.call_id == call.id)
        .order_by(CallParticipant.joined_at.asc())
    )
    raw_participants = (await db.execute(stmt_p)).scalars().all()

    # 3. Query pending / active CallTransfer records
    stmt_t = (
        select(CallTransfer)
        .where(
            and_(
                CallTransfer.call_id == call.id,
                CallTransfer.status.in_(["PENDING", "TARGET_RINGING", "ACCEPTED", "THREE_WAY_ACTIVE"])
            )
        )
        .order_by(CallTransfer.created_at.asc())
    )
    active_transfers = (await db.execute(stmt_t)).scalars().all()

    participants: List[Dict[str, Any]] = []
    seen_agent_ids = set()

    # -------------------------------------------------------------
    # Participant 1: Customer (Always First)
    # -------------------------------------------------------------
    cust_name = (
        f"{lead.first_name or ''} {lead.last_name or ''}".strip()
        if lead and (lead.first_name or lead.last_name)
        else (call.client_name or "Customer")
    )
    cust_phone = (lead.phone if lead else None) or call.client_phone or "Telephony Line"
    cust_part_record = next((p for p in raw_participants if p.participant_type == "CUSTOMER"), None)

    cust_status = "CONNECTED"
    if call.status in ["COMPLETED", "TERMINATED", "DISCONNECTED", "ENDED", "FAILED"]:
        cust_status = "LEFT"
    elif cust_part_record and cust_part_record.left_at:
        cust_status = "LEFT"

    participants.append({
        "id": cust_part_record.id if cust_part_record else f"cust-{call.id}",
        "participant_id": call.lead_id or "customer",
        "name": cust_name,
        "phone": cust_phone,
        "role": "CUSTOMER",
        "participant_type": "CUSTOMER",
        "role_label": "Customer / Lead",
        "status": cust_status,
        "is_host": False,
        "is_removable": False,
        "joined_at": _to_utc_iso(call.connected_at or call.created_at),
        "duration_seconds": max(0, int((now - (call.connected_at or call.created_at)).total_seconds())) if cust_status == "CONNECTED" else 0,
    })

    # -------------------------------------------------------------
    # Participant 2: Original Host Voice Agent
    # -------------------------------------------------------------
    host_user = None
    host_raw_id = call.agent_id or call.user_id
    if host_raw_id:
        host_user = await db.get(User, host_raw_id)
        if not host_user:
            host_exp = await db.get(Expert, host_raw_id)
            if host_exp and host_exp.user_id:
                host_user = await db.get(User, host_exp.user_id)

    host_name = (
        (host_user.full_name if host_user else None)
        or (host_user.email if host_user else None)
        or "Original Voice Agent (Host)"
    )
    host_id = host_raw_id or (host_user.id if host_user else "host-agent")
    seen_agent_ids.add(host_id)
    if host_user:
        seen_agent_ids.add(host_user.id)

    host_part_record = next(
        (p for p in raw_participants if p.participant_type == "AGENT" and (p.participant_id in seen_agent_ids)),
        None
    )

    host_status = "CONNECTED"
    if call.status in ["COMPLETED", "TERMINATED", "DISCONNECTED", "ENDED"]:
        host_status = "LEFT"
    elif host_part_record and host_part_record.left_at:
        host_status = "LEFT"

    participants.append({
        "id": host_part_record.id if host_part_record else f"host-{call.id}",
        "participant_id": host_id,
        "name": host_name,
        "phone": getattr(host_user, "phone", "") or "Voice Softphone",
        "role": "HOST_AGENT",
        "participant_type": "VOICE_AGENT",
        "role_label": "Original Voice Agent (Host)",
        "status": host_status,
        "is_host": True,
        "is_removable": False,
        "joined_at": _to_utc_iso(call.created_at),
        "duration_seconds": max(0, int((now - call.created_at).total_seconds())) if host_status == "CONNECTED" else 0,
    })

    # -------------------------------------------------------------
    # Additional Voice Agents (Agent 2, Agent 3...)
    # -------------------------------------------------------------
    for p in raw_participants:
        if p.participant_type == "AGENT" and p.participant_id and p.participant_id not in seen_agent_ids:
            seen_agent_ids.add(p.participant_id)
            u = await db.get(User, p.participant_id)
            exp = None
            if not u:
                exp = await db.get(Expert, p.participant_id)
                if exp and exp.user_id:
                    u = await db.get(User, exp.user_id)

            agent_name = (
                (u.full_name if u else None)
                or (exp.name if exp else None)
                or (u.email if u else None)
                or f"Voice Agent ({p.participant_id[:8]})"
            )

            p_status = p.status or "CONNECTED"
            if p.left_at:
                p_status = "LEFT"

            participants.append({
                "id": p.id,
                "participant_id": p.participant_id,
                "name": agent_name,
                "phone": getattr(u, "phone", "") or "Voice Softphone",
                "role": "VOICE_AGENT",
                "participant_type": "VOICE_AGENT",
                "role_label": f"Voice Agent ({agent_name})",
                "status": p_status,
                "is_host": False,
                "is_removable": True,
                "joined_at": _to_utc_iso(p.joined_at),
                "duration_seconds": max(0, int((now - p.joined_at).total_seconds())) if p_status == "CONNECTED" else p.duration_seconds,
            })

    # Also check any active transfers not yet in CallParticipant
    for t in active_transfers:
        if t.to_agent_id and t.to_agent_id not in seen_agent_ids:
            seen_agent_ids.add(t.to_agent_id)
            u = await db.get(User, t.to_agent_id)
            exp = None
            if not u:
                exp = await db.get(Expert, t.to_agent_id)
                if exp and exp.user_id:
                    u = await db.get(User, exp.user_id)

            agent_name = (
                (u.full_name if u else None)
                or (exp.name if exp else None)
                or (u.email if u else None)
                or f"Voice Agent ({t.to_agent_id[:8]})"
            )

            t_status = "INVITED" if t.status == "PENDING" else ("RINGING" if t.status == "TARGET_RINGING" else "CONNECTED")

            participants.append({
                "id": f"transfer-{t.id}",
                "participant_id": t.to_agent_id,
                "name": agent_name,
                "phone": getattr(u, "phone", "") or "Voice Softphone",
                "role": "VOICE_AGENT",
                "participant_type": "VOICE_AGENT",
                "role_label": f"Voice Agent ({agent_name})",
                "status": t_status,
                "is_host": False,
                "is_removable": True,
                "joined_at": _to_utc_iso(t.created_at),
                "duration_seconds": 0,
            })

    # Count active connected/invited participants
    active_count = len([p for p in participants if p["status"] in ["CONNECTED", "INVITED", "RINGING"]])
    connected_count = len([p for p in participants if p["status"] == "CONNECTED"])

    return {
        "call_id": call.id,
        "conference_sid": conf_sid,
        "call_status": call.status,
        "capacity": MAX_CONFERENCE_CAPACITY,
        "active_participants_count": active_count,
        "connected_count": connected_count,
        "is_full": active_count >= MAX_CONFERENCE_CAPACITY,
        "participants": participants,
    }


async def invite_voice_agent_to_conference(
    call_id: str,
    target_agent_id: str,
    inviter: User,
    db: AsyncSession,
    notes: Optional[str] = None
) -> Dict[str, Any]:
    """
    Invites a Voice Agent into the active conference.
    - Preserves Customer and Host Agent 1 connections simultaneously.
    - Prevents duplicate invitations / memberships.
    - Enforces max capacity limits.
    - Broadcasts WebSocket events to update all participants in real time.
    """
    call, conf_sid = await get_or_create_conference_room(call_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Capacity Check
    current_data = await list_conference_participants(call.id, db)
    if current_data["active_participants_count"] >= MAX_CONFERENCE_CAPACITY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Conference capacity reached (maximum {MAX_CONFERENCE_CAPACITY} participants allowed)."
        )

    # 2. Resolve Target Agent (User, Expert, or MgmtAgent)
    target_user = await db.get(User, target_agent_id)
    target_expert = None
    if not target_user:
        target_expert = await db.get(Expert, target_agent_id)
        if target_expert and target_expert.user_id:
            target_user = await db.get(User, target_expert.user_id)

    if not target_user and not target_expert:
        # Search User by email or username
        stmt = select(User).where(or_(User.id == target_agent_id, User.email == target_agent_id))
        target_user = (await db.execute(stmt)).scalars().first()

    if not target_user and not target_expert:
        # Search Expert by email or name
        stmt_e = select(Expert).where(or_(Expert.email == target_agent_id, Expert.name == target_agent_id))
        target_expert = (await db.execute(stmt_e)).scalars().first()
        if target_expert and target_expert.user_id:
            target_user = await db.get(User, target_expert.user_id)

    if not target_user and not target_expert:
        logger.warning(
            f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
            f"status_code=404 reason='Voice Agent not found'"
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Voice Agent with ID '{target_agent_id}' not found."
        )

    # 2b. Organization / Tenant Isolation Check
    if inviter.role != "SUPER_ADMIN":
        if call.business_id and inviter.business_id and call.business_id != inviter.business_id:
            logger.warning(
                f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' inviter_biz='{inviter.business_id}' "
                f"call_biz='{call.business_id}' status_code=404 reason='Call not found in organization'"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Call '{call_id}' not found in your organization."
            )
        target_biz = getattr(target_user, "business_id", None) or getattr(target_expert, "business_id", None)
        if target_biz and call.business_id and target_biz != call.business_id:
            logger.warning(
                f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
                f"target_biz='{target_biz}' call_biz='{call.business_id}' status_code=404 reason='Voice agent belongs to another organization'"
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Voice Agent with ID '{target_agent_id}' not found in your organization."
            )

    target_user_id = target_user.id if target_user else target_agent_id
    target_name = (
        (target_expert.name if target_expert else None)
        or (target_user.full_name if target_user else None)
        or (target_user.email if target_user else None)
        or "Voice Agent"
    )

    logger.info(
        f"[CONFERENCE_INVITE] route='invite-agent' call_id='{call_id}' resolved_call_id='{call.id}' "
        f"conference_sid='{conf_sid}' target_agent_id='{target_agent_id}' resolved_agent_id='{target_user_id}' "
        f"agent_name='{target_name}' inviter='{inviter.id}'"
    )

    # 3. Prevent Self-Invite
    if target_user_id == inviter.id or (call.agent_id and target_user_id == call.agent_id):
        logger.warning(
            f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
            f"status_code=400 reason='Self-invite not allowed'"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot add yourself or the original host agent again."
        )

    # 4. Duplicate Check
    stmt_check = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == target_user_id,
            CallParticipant.left_at.is_(None),
            CallParticipant.status.in_(["CONNECTED", "INVITED", "RINGING"])
        )
    )
    existing_participant = (await db.execute(stmt_check)).scalars().first()
    if existing_participant:
        logger.warning(
            f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
            f"status_code=409 reason='Already in conference'"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voice Agent '{target_name}' is already connected or invited to this conference."
        )

    # Check pending CallTransfer
    stmt_t_check = select(CallTransfer).where(
        and_(
            CallTransfer.call_id == call.id,
            CallTransfer.to_agent_id == target_user_id,
            CallTransfer.status.in_(["PENDING", "TARGET_RINGING", "ACCEPTED"])
        )
    )
    existing_transfer = (await db.execute(stmt_t_check)).scalars().first()
    if existing_transfer:
        logger.warning(
            f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
            f"status_code=409 reason='Pending invitation exists'"
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Voice Agent '{target_name}' already has a pending invitation to this conference."
        )

    # 5. Check Target Agent Status (reject offline agents)
    target_status = getattr(target_user, "status", None) or (target_expert.status if target_expert else "available")
    if str(target_status).lower() in ["offline", "inactive", "disabled"]:
        logger.warning(
            f"[CONFERENCE_INVITE_FAIL] route='invite-agent' call_id='{call_id}' target_agent_id='{target_agent_id}' "
            f"status_code=400 reason='Agent is offline'"
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Voice Agent '{target_name}' is currently offline and cannot be added."
        )

    # 6. Ensure Call is in 3-Way Conference Active state
    call.conference_sid = conf_sid
    call.status = "CONFERENCE_3WAY_ACTIVE"
    db.add(call)

    # 7. Ensure Host Agent has a connected CallParticipant record
    host_id = call.agent_id or inviter.id
    stmt_host_p = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == host_id,
            CallParticipant.left_at.is_(None)
        )
    )
    host_p = (await db.execute(stmt_host_p)).scalars().first()
    if not host_p:
        host_p = CallParticipant(
            id=str(uuid.uuid4()),
            call_id=call.id,
            participant_type="AGENT",
            participant_id=host_id,
            phone_number=getattr(inviter, "phone", "") or "",
            status="CONNECTED",
            joined_at=call.created_at or now,
        )
        db.add(host_p)

    # 8. Ensure Customer has a connected CallParticipant record
    cust_id = call.lead_id or call.client_phone or "customer"
    stmt_cust_p = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_type == "CUSTOMER",
            CallParticipant.left_at.is_(None)
        )
    )
    cust_p = (await db.execute(stmt_cust_p)).scalars().first()
    if not cust_p:
        cust_p = CallParticipant(
            id=str(uuid.uuid4()),
            call_id=call.id,
            participant_type="CUSTOMER",
            participant_id=cust_id,
            phone_number=call.client_phone or "",
            status="CONNECTED",
            joined_at=call.connected_at or call.created_at or now,
        )
        db.add(cust_p)

    # 9. Create target agent CallParticipant record (INVITED)
    participant_record = CallParticipant(
        id=str(uuid.uuid4()),
        call_id=call.id,
        participant_type="AGENT",
        participant_id=target_user_id,
        phone_number=getattr(target_user, "phone", "") or "",
        status="INVITED",
        joined_at=now,
    )
    db.add(participant_record)

    # 10. Create CallTransfer record for the invitation
    biz_id = inviter.business_id or call.business_id or "default"
    transfer = CallTransfer(
        id=str(uuid.uuid4()),
        business_id=biz_id,
        call_id=call.id,
        lead_id=call.lead_id,
        from_agent_id=inviter.id,
        to_agent_id=target_user_id,
        transfer_type="warm",
        status="PENDING",
        created_at=now,
        updated_at=now,
    )
    db.add(transfer)

    # Audit log entry
    audit = AuditLog(
        user_id=inviter.id,
        action="CONFERENCE_AGENT_INVITED",
        entity="Call",
        entity_id=call.id,
        details={
            "conference_sid": conf_sid,
            "target_agent_id": target_user_id,
            "target_agent_name": target_name,
            "transfer_id": transfer.id,
            "notes": notes or "",
        },
    )
    db.add(audit)

    await db.commit()
    await db.refresh(call)

    # 11. Cross-register session in barge_sessions so Agent 2 audio can bridge immediately upon answer
    try:
        from app.api.routes.barge_stream import _get_or_create_session, barge_sessions
        session = await _get_or_create_session(call.id)
        session["status"] = "CONFERENCE_3WAY_ACTIVE"
        session["is_3way"] = True
        session["running"] = True
        for alias_k in [conf_sid, call.call_sid, transfer.id, target_user_id]:
            if alias_k:
                barge_sessions[alias_k] = session
                session.setdefault("aliases", set()).add(alias_k)
    except Exception as e:
        logger.debug(f"[CONF_INVITE_SESSION] Note on session bridge: {e}")

    # 12. Broadcast Real-time WebSocket Notifications
    # Fetch lead details for preview
    lead_obj = await db.get(Lead, call.lead_id) if call.lead_id else None
    lead_summary = {
        "id": lead_obj.id if lead_obj else "",
        "name": f"{lead_obj.first_name or ''} {lead_obj.last_name or ''}".strip() if lead_obj else (call.client_name or "Customer"),
        "phone": (lead_obj.phone if lead_obj else None) or call.client_phone or "",
        "company": getattr(lead_obj, "company", "") if lead_obj else "",
    }

    # A. Targeted invite event for target agent's UI
    invite_event = {
        "event": "incoming_voice_agent_transfer",
        "type": "incoming_voice_agent_transfer",
        "is_conference_invite": True,
        "transfer_id": transfer.id,
        "call_id": call.id,
        "call_session_id": call.id,
        "conference_sid": conf_sid,
        "from_agent_id": inviter.id,
        "from_agent_name": inviter.full_name or inviter.email or "Voice Agent",
        "to_agent_id": target_user_id,
        "to_voice_agent_id": target_user_id,
        "transfer_type": "warm",
        "is_warm": True,
        "lead": lead_summary,
        "notes": notes or f"Invited to ongoing conference call with {lead_summary['name']}.",
        "created_at": now_iso,
    }
    await global_ws_manager.broadcast_to_user(target_user_id, invite_event)

    # B. Broadcast conference participants update to all clients
    updated_data = await list_conference_participants(call.id, db)
    conf_event = {
        "event": "CONFERENCE_PARTICIPANTS_UPDATED",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "status": "CONFERENCE_3WAY_ACTIVE",
        "capacity": MAX_CONFERENCE_CAPACITY,
        "participants": updated_data["participants"],
        "active_participants_count": updated_data["active_participants_count"],
        "message": f"Voice Agent '{target_name}' invited to conference.",
    }
    await global_ws_manager.broadcast_json(conf_event)

    return {
        "success": True,
        "status": "INVITED",
        "message": f"Voice Agent '{target_name}' invited to conference.",
        "conference_sid": conf_sid,
        "call_id": call.id,
        "transfer_id": transfer.id,
        "target_agent_id": target_user_id,
        "target_agent_name": target_name,
        "participants": updated_data["participants"],
    }


async def accept_conference_invite(call_id: str, agent_user: User, db: AsyncSession) -> Dict[str, Any]:
    """
    Called when an invited Voice Agent accepts and joins the ongoing conference call.
    - Bridges the agent into the conference without disconnecting customer or Agent 1.
    - Updates participant state to CONNECTED.
    """
    call, conf_sid = await get_or_create_conference_room(call_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Find pending transfer
    stmt_t = select(CallTransfer).where(
        and_(
            CallTransfer.call_id == call.id,
            CallTransfer.to_agent_id == agent_user.id,
            CallTransfer.status.in_(["PENDING", "TARGET_RINGING"])
        )
    ).order_by(CallTransfer.created_at.desc())
    transfer = (await db.execute(stmt_t)).scalars().first()

    if transfer:
        transfer.status = "ACCEPTED"
        transfer.connected_at = now
        transfer.updated_at = now
        db.add(transfer)

    # Find CallParticipant record
    stmt_p = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == agent_user.id
        )
    ).order_by(CallParticipant.joined_at.desc())
    participant = (await db.execute(stmt_p)).scalars().first()

    if not participant:
        participant = CallParticipant(
            id=str(uuid.uuid4()),
            call_id=call.id,
            participant_type="AGENT",
            participant_id=agent_user.id,
            phone_number=getattr(agent_user, "phone", "") or "",
            status="CONNECTED",
            joined_at=now,
        )
    else:
        participant.status = "CONNECTED"
        participant.joined_at = now
        participant.left_at = None

    db.add(participant)
    call.status = "CONFERENCE_3WAY_ACTIVE"
    db.add(call)

    # Audit log
    audit = AuditLog(
        user_id=agent_user.id,
        action="CONFERENCE_AGENT_JOINED",
        entity="Call",
        entity_id=call.id,
        details={
            "conference_sid": conf_sid,
            "agent_id": agent_user.id,
            "agent_name": agent_user.full_name or agent_user.email,
        },
    )
    db.add(audit)
    await db.commit()

    # Ensure barge_sessions audio bridge is configured
    try:
        from app.api.routes.barge_stream import _get_or_create_session, barge_sessions, flush_barge_session_queues_for_transfer
        flush_barge_session_queues_for_transfer(call.id)
        session = await _get_or_create_session(call.id)
        session["status"] = "CONFERENCE_3WAY_ACTIVE"
        session["is_3way"] = True
        session["running"] = True
        for k in [conf_sid, call.call_sid, agent_user.id]:
            if k:
                barge_sessions[k] = session
                session.setdefault("aliases", set()).add(k)
    except Exception as e:
        logger.debug(f"[CONF_ACCEPT_SESSION] Note on session bridge: {e}")

    # Broadcast real-time participants update
    updated_data = await list_conference_participants(call.id, db)
    conf_event = {
        "event": "CONFERENCE_PARTICIPANTS_UPDATED",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "status": "CONFERENCE_3WAY_ACTIVE",
        "capacity": MAX_CONFERENCE_CAPACITY,
        "participants": updated_data["participants"],
        "active_participants_count": updated_data["active_participants_count"],
        "message": f"Voice Agent '{agent_user.full_name or agent_user.email}' joined the conference.",
    }
    await global_ws_manager.broadcast_json(conf_event)

    # Also notify transfer accepted.
    # CRITICAL: include from_agent_id / from_voice_agent_id so Agent 1's isSource
    # check in page.tsx (line 443-447) evaluates to True and Agent 1 calls
    # startBargeIn to keep its audio bridge active for the 3-way conference.
    from_agent_id_val = (transfer.from_agent_id if transfer else None) or call.agent_id or call.user_id
    await global_ws_manager.broadcast_json({
        "event": "voice_agent_transfer_accepted",
        "call_id": call.id,
        "call_session_id": call.id,
        "conference_sid": conf_sid,
        "from_agent_id": from_agent_id_val,
        "from_voice_agent_id": from_agent_id_val,
        "to_agent_id": agent_user.id,
        "to_voice_agent_id": agent_user.id,
        "to_agent_name": agent_user.full_name or agent_user.email,
        "transfer_type": "warm",
        "is_3way": True,
        "status": "CONFERENCE_3WAY_ACTIVE",
    })

    return {
        "success": True,
        "status": "CONNECTED",
        "message": "Joined conference successfully.",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "participants": updated_data["participants"],
    }


async def reject_conference_invite(
    call_id: str,
    agent_user: User,
    db: AsyncSession,
    reason: str = "Declined"
) -> Dict[str, Any]:
    """
    Called when an invited Voice Agent declines or times out.
    - Customer and Host Agent 1 remain connected without interruption.
    - Updates participant record to FAILED.
    """
    call, conf_sid = await get_or_create_conference_room(call_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    stmt_t = select(CallTransfer).where(
        and_(
            CallTransfer.call_id == call.id,
            CallTransfer.to_agent_id == agent_user.id,
            CallTransfer.status.in_(["PENDING", "TARGET_RINGING"])
        )
    ).order_by(CallTransfer.created_at.desc())
    transfer = (await db.execute(stmt_t)).scalars().first()

    if transfer:
        transfer.status = "REJECTED"
        transfer.updated_at = now
        db.add(transfer)

    stmt_p = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            CallParticipant.participant_id == agent_user.id
        )
    ).order_by(CallParticipant.joined_at.desc())
    participant = (await db.execute(stmt_p)).scalars().first()

    if participant:
        participant.status = "FAILED"
        participant.left_at = now
        db.add(participant)

    await db.commit()

    # Broadcast participants update so UI clears the connecting agent and shows failure notice
    updated_data = await list_conference_participants(call.id, db)
    conf_event = {
        "event": "CONFERENCE_PARTICIPANTS_UPDATED",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "status": call.status,
        "capacity": MAX_CONFERENCE_CAPACITY,
        "participants": updated_data["participants"],
        "active_participants_count": updated_data["active_participants_count"],
        "message": f"Voice Agent '{agent_user.full_name or agent_user.email}' declined the invitation: {reason}",
    }
    await global_ws_manager.broadcast_json(conf_event)

    return {
        "success": True,
        "status": "REJECTED",
        "message": f"Conference invite declined: {reason}",
        "call_id": call.id,
        "participants": updated_data["participants"],
    }


async def remove_conference_participant(
    call_id: str,
    participant_id: str,
    requester: User,
    db: AsyncSession
) -> Dict[str, Any]:
    """
    Removes an eligible participant (e.g. secondary Voice Agent) from the conference.
    - Customer and Host Agent 1 remain live on the call.
    - Cleanly closes the removed participant's audio session without killing the bridge.
    """
    call, conf_sid = await get_or_create_conference_room(call_id, db)
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # 1. Look up CallParticipant record
    stmt_p = select(CallParticipant).where(
        and_(
            CallParticipant.call_id == call.id,
            or_(
                CallParticipant.id == participant_id,
                CallParticipant.participant_id == participant_id,
            ),
            CallParticipant.left_at.is_(None)
        )
    )
    participant = (await db.execute(stmt_p)).scalars().first()

    if not participant:
        # Check if it was an invited transfer not yet in participant
        stmt_t = select(CallTransfer).where(
            and_(
                CallTransfer.call_id == call.id,
                or_(
                    CallTransfer.id == participant_id,
                    CallTransfer.to_agent_id == participant_id,
                ),
                CallTransfer.status.in_(["PENDING", "TARGET_RINGING", "ACCEPTED"])
            )
        )
        transfer = (await db.execute(stmt_t)).scalars().first()
        if transfer:
            transfer.status = "CANCELLED"
            transfer.disconnected_at = now
            transfer.updated_at = now
            db.add(transfer)
            await db.commit()

            updated_data = await list_conference_participants(call.id, db)
            conf_event = {
                "event": "CONFERENCE_PARTICIPANTS_UPDATED",
                "call_id": call.id,
                "conference_sid": conf_sid,
                "status": call.status,
                "capacity": MAX_CONFERENCE_CAPACITY,
                "participants": updated_data["participants"],
                "active_participants_count": updated_data["active_participants_count"],
                "message": "Invitation cancelled.",
            }
            await global_ws_manager.broadcast_json(conf_event)
            return {
                "success": True,
                "message": "Invitation cancelled.",
                "participants": updated_data["participants"],
            }

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active conference participant '{participant_id}' not found."
        )

    # Safety: Cannot remove customer or host agent
    if participant.participant_type == "CUSTOMER":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove the customer from the conference. End the call to hang up."
        )

    if call.agent_id and participant.participant_id == call.agent_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The original host agent cannot be removed from the conference."
        )

    # Mark left
    participant.left_at = now
    participant.status = "LEFT"
    if participant.joined_at:
        participant.duration_seconds = max(0, int((now - participant.joined_at).total_seconds()))
    db.add(participant)

    # Update any matching CallTransfer
    stmt_t2 = select(CallTransfer).where(
        and_(
            CallTransfer.call_id == call.id,
            CallTransfer.to_agent_id == participant.participant_id,
            CallTransfer.status.in_(["ACCEPTED", "PENDING", "TARGET_RINGING"])
        )
    )
    t2 = (await db.execute(stmt_t2)).scalars().first()
    if t2:
        t2.status = "COMPLETED"
        t2.disconnected_at = now
        t2.updated_at = now
        db.add(t2)

    # Audit log
    audit = AuditLog(
        user_id=requester.id,
        action="CONFERENCE_PARTICIPANT_REMOVED",
        entity="Call",
        entity_id=call.id,
        details={
            "conference_sid": conf_sid,
            "removed_participant_id": participant.participant_id,
        },
    )
    db.add(audit)
    await db.commit()

    # Gracefully notify and detach softphone audio for removed participant
    try:
        from app.api.routes.barge_stream import barge_sessions
        s = barge_sessions.get(call.id)
        if s and participant.participant_id:
            # We don't terminate s, just drop aliases or let other bargers continue
            barge_sessions.pop(participant.participant_id, None)
    except Exception as e:
        logger.debug(f"[CONF_REMOVE_AUDIO] Note on audio session cleanup: {e}")

    # Notify removed user
    if participant.participant_id:
        await global_ws_manager.broadcast_to_user(participant.participant_id, {
            "event": "CONFERENCE_PARTICIPANT_LEFT",
            "call_id": call.id,
            "conference_sid": conf_sid,
            "reason": "Removed by conference host",
        })

    # Broadcast updated participants
    updated_data = await list_conference_participants(call.id, db)
    conf_event = {
        "event": "CONFERENCE_PARTICIPANTS_UPDATED",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "status": call.status,
        "capacity": MAX_CONFERENCE_CAPACITY,
        "participants": updated_data["participants"],
        "active_participants_count": updated_data["active_participants_count"],
        "message": "Participant removed from conference.",
    }
    await global_ws_manager.broadcast_json(conf_event)

    return {
        "success": True,
        "status": "LEFT",
        "message": "Participant removed from conference.",
        "call_id": call.id,
        "conference_sid": conf_sid,
        "participants": updated_data["participants"],
    }
