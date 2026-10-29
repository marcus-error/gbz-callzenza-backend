import io
import csv
import re
import json
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy import select, func, or_, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_db_user
from app.db.database import get_db
from app.core.timezone import (
    APP_TIMEZONE,
    UTC_TIMEZONE,
    get_ist_day_bounds_in_utc,
    to_utc_iso,
    to_ist_time_str,
    to_ist_datetime_str,
    get_current_ist_time,
)
from app.db.models import (
    User,
    Call,
    Lead,
    Campaign,
    CampaignLead,
    LeadAssignment,
    AgentAssignmentQueue,
    ClientConfig,
    ParkedCall,
    Expert,
    Client,
    Business,
    AIAgent,
    CallTransfer,
    CallParticipant,
    ParkedConference,
    LeadGroup
)
from app.services.voice_service import add_client_to_conference_call
from app.core.config import get_settings

settings = get_settings()

router = APIRouter(prefix="/api/voice", tags=["agent_dashboard"])


# ---------------------------------------------------------------------------
# Pydantic Schemas
# ---------------------------------------------------------------------------

class AgentStatusUpdate(BaseModel):
    status: str  # AVAILABLE, BUSY, ON CALL, PAUSED, OFFLINE, ON BREAK


class ClientConfigIn(BaseModel):
    slot: str  # D1, D2, D3
    client_id: Optional[str] = None
    client_name: Optional[str] = ""
    client_phone: Optional[str] = ""


class BatchClientConfigIn(BaseModel):
    D1: Optional[str] = None
    D2: Optional[str] = None
    D3: Optional[str] = None
    slot_mappings: Optional[dict[str, Optional[str]]] = None


class InitiateCallRequest(BaseModel):
    lead_id: Optional[str] = None
    phone_number: Optional[str] = None
    campaign_id: Optional[str] = None
    client_name: Optional[str] = None
    client_phone: Optional[str] = None
    agent_id: Optional[str] = None
    agent_type: Optional[str] = None
    sip_account_id: Optional[str] = None


class ParkCallRequest(BaseModel):
    slot: Optional[str] = None
    hold_reason: Optional[str] = "PARKED"


class ConnectClientRequest(BaseModel):
    slot: Optional[str] = None
    client_id: Optional[str] = None
    client_phone: Optional[str] = None
    client_name: Optional[str] = None


class ConferenceParticipantInfo(BaseModel):
    agentId: Optional[str] = None
    clientId: Optional[str] = None
    agent_id: Optional[str] = None
    client_id: Optional[str] = None
    slot: Optional[str] = None
    name: Optional[str] = None
    phone: Optional[str] = None


class ParkedConferenceRequest(BaseModel):
    D1: Optional[str] = ""
    D2: Optional[str] = ""
    D3: Optional[str] = ""


class TransferCallRequest(BaseModel):
    destination_agent_id: Optional[str] = None
    destination_number: Optional[str] = None
    client_id: Optional[str] = None
    target_agent_id: Optional[str] = None
    transfer_type: Optional[str] = "three_way"
    slot_mappings: Optional[dict[str, Optional[str]]] = None
    participants: Optional[list[ConferenceParticipantInfo]] = None


class EndCallRequest(BaseModel):
    duration_seconds: Optional[int] = 0
    outcome: Optional[str] = "Completed"
    qualification_status: Optional[str] = None
    lead_id: Optional[str] = None


# ---------------------------------------------------------------------------
# 1. Dashboard Stats
# ---------------------------------------------------------------------------

@router.get("/dashboard/stats")
async def get_dashboard_stats(
    campaign_id: Optional[str] = Query(None),
    lead_group_id: Optional[str] = Query(None),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    # Resolve Expert ID for logged in user if available
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    expert_id = expert.id if expert else user.id
    agent_ids = list(set(filter(None, [user.id, expert_id])))

    # Also resolve calls where this agent was a participant (e.g. transfers, conferences)
    part_stmt = select(CallParticipant.call_id).where(
        CallParticipant.participant_id.in_(agent_ids),
        CallParticipant.left_at.is_(None)
    )
    part_call_ids = [pid for pid in (await db.execute(part_stmt)).scalars().all() if pid]

    # Resolve active warm transfer calls for this agent
    xfer_stmt = select(CallTransfer.call_id).where(
        or_(CallTransfer.from_agent_id.in_(agent_ids), CallTransfer.to_agent_id.in_(agent_ids)),
        CallTransfer.status.in_(["ACCEPTED", "TARGET_CONNECTED", "PENDING"])
    )
    xfer_call_ids = [cid for cid in (await db.execute(xfer_stmt)).scalars().all() if cid]
    all_member_call_ids = list(set(part_call_ids + xfer_call_ids))

    # Call filter condition: strictly scoped to user.business_id
    user_biz = user.business_id
    is_admin_or_sup = getattr(user, "role", None) in ["ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER"]

    if not user_biz and getattr(user, "role", None) != "SUPER_ADMIN":
        return {
            "total_calls": 0,
            "total_time": 0,
            "formatted_total_time": "00:00:00",
            "today_time": 0,
            "formatted_today_time": "00:00:00",
            "today_calls": 0,
            "calls_in_queue": 0,
            "calls_on_hold": 0,
            "agent_status": user.status or "AVAILABLE",
            "active_call": None,
        }

    if is_admin_or_sup:
        call_filter = (Call.business_id == user_biz) if user_biz else (Call.id.isnot(None))
    else:
        agent_conds = [Call.agent_id.in_(agent_ids), Call.user_id == user.id]
        if all_member_call_ids:
            agent_conds.append(Call.id.in_(all_member_call_ids))
        call_filter = and_(Call.business_id == user_biz, or_(*agent_conds)) if user_biz else or_(*agent_conds)

    # Sanitize campaign_id so strings like "undefined", "null", or Query objects are not passed
    clean_campaign_id = campaign_id if (isinstance(campaign_id, str) and campaign_id.strip() and campaign_id not in ("undefined", "null")) else None

    # 1. Total calls handled by logged in agent (filtered by campaign_id if specified)
    stmt_total_calls = select(func.count(Call.id)).where(call_filter)
    if clean_campaign_id:
        stmt_total_calls = stmt_total_calls.where(Call.campaign_id == clean_campaign_id)
    total_calls = (await db.execute(stmt_total_calls)).scalar() or 0

    # 2. Total time in seconds
    stmt_total_time = select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(call_filter)
    if clean_campaign_id:
        stmt_total_time = stmt_total_time.where(Call.campaign_id == clean_campaign_id)
    total_time_seconds = (await db.execute(stmt_total_time)).scalar() or 0

    # 3. Today's total time in seconds (calculated according to Asia/Kolkata day bounds)
    today_start, _, _ = get_ist_day_bounds_in_utc(None)
    stmt_today_time = select(func.coalesce(func.sum(Call.duration_seconds), 0)).where(
        and_(
            call_filter,
            Call.created_at >= today_start
        )
    )
    if clean_campaign_id:
        stmt_today_time = stmt_today_time.where(Call.campaign_id == clean_campaign_id)
    today_time_seconds = (await db.execute(stmt_today_time)).scalar() or 0

    # Today's total calls
    stmt_today_calls = select(func.count(Call.id)).where(
        and_(
            call_filter,
            Call.created_at >= today_start
        )
    )
    if clean_campaign_id:
        stmt_today_calls = stmt_today_calls.where(Call.campaign_id == clean_campaign_id)
    today_calls = (await db.execute(stmt_today_calls)).scalar() or 0

    # 4. Calls in queue (incoming transfer/queue requests waiting for this agent or business)
    stmt_transfer_q = select(func.count(CallTransfer.id)).where(
        and_(
            CallTransfer.to_agent_id.in_(agent_ids),
            CallTransfer.status.in_(["PENDING", "WAITING", "TARGET_RINGING"])
        )
    )
    if user_biz:
        stmt_transfer_q = stmt_transfer_q.where(CallTransfer.business_id == user_biz)
    transfer_q_count = (await db.execute(stmt_transfer_q)).scalar() or 0

    stmt_assign_q = select(func.count(AgentAssignmentQueue.id)).where(
        and_(
            AgentAssignmentQueue.status == "WAITING"
        )
    )
    if user_biz:
        stmt_assign_q = stmt_assign_q.where(
            AgentAssignmentQueue.business_id == user_biz,
            or_(
                AgentAssignmentQueue.requested_expert_id.in_(agent_ids),
                AgentAssignmentQueue.assigned_agent_id.in_(agent_ids),
                AgentAssignmentQueue.requested_expert_id.is_(None),
            )
        )
    else:
        stmt_assign_q = stmt_assign_q.where(
            or_(
                AgentAssignmentQueue.requested_expert_id.in_(agent_ids),
                AgentAssignmentQueue.assigned_agent_id.in_(agent_ids),
            )
        )
    assign_q_count = (await db.execute(stmt_assign_q)).scalar() or 0

    calls_in_queue = transfer_q_count + assign_q_count

    # 5. Calls on hold / parked for logged in agent or business
    stmt_hold_count = select(func.count(ParkedCall.id)).where(
        and_(
            ParkedCall.status == "ON_HOLD"
        )
    )
    if user_biz:
        stmt_hold_count = stmt_hold_count.where(ParkedCall.business_id == user_biz)
    else:
        stmt_hold_count = stmt_hold_count.where(ParkedCall.agent_id.in_(agent_ids))
    calls_on_hold = (await db.execute(stmt_hold_count)).scalar() or 0

    # 6. Agent status (Dynamic based on active calls, on-hold calls, or DB user status)
    stmt_active_calls = select(Call).where(
        and_(
            call_filter,
            Call.status.in_(["IN_PROGRESS", "RINGING", "CLIENT_ADDING", "CONFERENCE_3WAY_ACTIVE", "HUMAN_HANDLING", "TRANSFER_REQUESTED"])
        )
    )
    all_active = (await db.execute(stmt_active_calls)).scalars().all()
    valid_active_count = len(all_active)

    if valid_active_count > 0:
        agent_status = "ON CALL"
    elif calls_on_hold > 0:
        agent_status = "ON HOLD"
    else:
        if user.status == "ON CALL":
            user.status = "AVAILABLE"
            db.add(user)
            await db.commit()
        # Default to PAUSED — agent must manually START to become AVAILABLE
        agent_status = user.status or "PAUSED"

    return {
        "total_calls": total_calls,
        "today_calls": today_calls,
        "total_time_seconds": total_time_seconds,
        "today_time_seconds": today_time_seconds,
        "calls_in_queue": calls_in_queue,
        "calls_on_hold": calls_on_hold,
        "agent_status": agent_status,
    }


# ---------------------------------------------------------------------------
# 2. My Calls History Detailed View
# ---------------------------------------------------------------------------

@router.get("/calls/my-calls")
async def get_my_calls_history(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    expert_id = expert.id if expert else user.id
    agent_ids = list(set(filter(None, [user.id, expert_id])))

    user_biz = user.business_id
    if not user_biz and (user.role or "").upper() != "SUPER_ADMIN":
        return []

    is_admin_or_sup = (user.role or "").upper() in ["ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER"]
    if is_admin_or_sup:
        call_filter = (Call.business_id == user_biz) if user_biz else (Call.id.isnot(None))
    else:
        call_filter = and_(
            Call.business_id == user_biz,
            or_(Call.agent_id.in_(agent_ids), Call.user_id == user.id)
        ) if user_biz else or_(Call.agent_id.in_(agent_ids), Call.user_id == user.id)

    stmt = (
        select(Call, Lead, Campaign, Client)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .outerjoin(Campaign, Call.campaign_id == Campaign.id)
        .outerjoin(Client, or_(Call.client_id == Client.id, Campaign.client_id == Client.id))
        .where(call_filter)
        .order_by(desc(Call.created_at))
    )
    results = (await db.execute(stmt)).all()

    call_logs = []
    seen_call_ids = set()
    for row in results:
        call_obj: Call = row[0]
        if call_obj.id in seen_call_ids:
            continue
        seen_call_ids.add(call_obj.id)
        lead_obj: Optional[Lead] = row[1]
        campaign_obj: Optional[Campaign] = row[2]
        client_obj: Optional[Client] = row[3]

        customer_name = f"{lead_obj.first_name} {lead_obj.last_name}".strip() if lead_obj else "Customer"
        if not customer_name:
            customer_name = "Unknown Customer"

        customer_phone = call_obj.to_number or (lead_obj.phone if lead_obj else "")
        lead_name = customer_name
        campaign_name = campaign_obj.name if campaign_obj else ""
        client_name = client_obj.name if client_obj else (campaign_obj.client_name if campaign_obj else call_obj.client_name)
        agent_name = user.full_name or "Agent"

        start_time = call_obj.initiated_at or call_obj.created_at
        connected_time = call_obj.connected_at
        end_time = call_obj.ended_at or (start_time + timedelta(seconds=call_obj.duration_seconds or 0))
        duration_sec = call_obj.duration_seconds or 0
        minutes = duration_sec // 60
        seconds = duration_sec % 60
        formatted_duration = f"{minutes:02d}:{seconds:02d}"

        client_dur_sec = call_obj.client_duration_seconds or 0
        cl_min = client_dur_sec // 60
        cl_sec = client_dur_sec % 60
        formatted_client_dur = f"{cl_min:02d}:{cl_sec:02d}"

        call_status = call_obj.status or "COMPLETED"
        outcome = call_obj.call_result or ("Connected" if duration_sec > 0 else "No Answer")
        connected = duration_sec > 0 or call_status in ("COMPLETED", "HUMAN_HANDLING", "CONFERENCE_3WAY_ACTIVE")
        qualification_status = lead_obj.qualification_status if lead_obj else "UNQUALIFIED"

        recording_url = call_obj.recording_url or ""
        if not recording_url:
            clean_status = (call_obj.status or "").upper().strip()
            if clean_status not in ("FAILED", "BUSY", "NO_ANSWER", "NO-ANSWER", "MISSED", "CANCELED", "CANCELLED", "REJECTED") and (duration_sec > 0 or call_obj.transcript):
                from app.services.recording_service import ensure_call_recording
                recording_url = ensure_call_recording(call_obj.id, duration_sec=duration_sec or 15)
                if recording_url:
                    call_obj.recording_url = recording_url
                    db.add(call_obj)

        call_logs.append({
            "id": call_obj.id,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "lead_name": lead_name,
            "campaign_name": campaign_name,
            "agent_name": agent_name,
            "start_time": to_ist_time_str(start_time),
            "connected_time": to_ist_time_str(connected_time) if connected_time else "—",
            "end_time": to_ist_time_str(end_time),
            "date_time": to_ist_datetime_str(start_time),
            "duration_seconds": duration_sec,
            "formatted_duration": formatted_duration,
            "client_name": client_name or "",
            "client_phone": call_obj.client_phone or "",
            "client_duration_seconds": client_dur_sec,
            "formatted_client_duration": formatted_client_dur,
            "status": call_status,
            "outcome": outcome,
            "connected": connected,
            "qualification_status": qualification_status,
            "recording_url": recording_url,
            "record_id": f"REC-{call_obj.id[:8].upper()}",
            "has_recording": True,
        })

    return call_logs


# ---------------------------------------------------------------------------
# 3. Total Time Statistics & Breakdown
# ---------------------------------------------------------------------------

@router.get("/dashboard/total-time")
async def get_total_time_breakdown(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    expert_id = expert.id if expert else user.id
    agent_ids = list(set(filter(None, [user.id, expert_id])))

    # Also resolve calls where this agent was a participant (e.g. transfers, conferences)
    part_stmt = select(CallParticipant.call_id).where(CallParticipant.participant_id.in_(agent_ids))
    part_call_ids = [pid for pid in (await db.execute(part_stmt)).scalars().all() if pid]

    user_biz = user.business_id
    if not user_biz and (user.role or "").upper() != "SUPER_ADMIN":
        return {
            "total_time_formatted": "00:00:00",
            "total_seconds": 0,
            "today_time_formatted": "00:00:00",
            "today_seconds": 0,
            "calls_count": 0,
            "calls": [],
            "daily_breakdown": []
        }

    is_admin_or_sup = (user.role or "").upper() in ["ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER"]
    if is_admin_or_sup:
        call_filter = (Call.business_id == user_biz) if user_biz else (Call.id.isnot(None))
    else:
        agent_conds = [Call.agent_id.in_(agent_ids), Call.user_id == user.id]
        if part_call_ids:
            agent_conds.append(Call.id.in_(part_call_ids))
        call_filter = and_(Call.business_id == user_biz, or_(*agent_conds)) if user_biz else or_(*agent_conds)

    stmt = select(Call, Lead).outerjoin(Lead, Call.lead_id == Lead.id).where(call_filter).order_by(desc(Call.created_at))
    results = (await db.execute(stmt)).all()

    total_seconds = 0
    today_seconds = 0
    today_date = get_current_ist_time().date()

    individual_calls = []
    date_map = {}

    for row in results:
        call_obj: Call = row[0]
        lead_obj: Optional[Lead] = row[1]

        dur = call_obj.duration_seconds or 0
        total_seconds += dur

        created_dt = call_obj.created_at
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=UTC_TIMEZONE)
        call_dt_ist = created_dt.astimezone(APP_TIMEZONE)
        call_date = call_dt_ist.date()

        if call_date == today_date:
            today_seconds += dur

        customer_name = f"{lead_obj.first_name} {lead_obj.last_name}".strip() if lead_obj else "Customer"

        minutes = dur // 60
        seconds = dur % 60
        fmt_dur = f"{minutes:02d}:{seconds:02d}"

        individual_calls.append({
            "call_id": call_obj.id,
            "customer_name": customer_name or "Unknown",
            "customer_phone": call_obj.to_number or (lead_obj.phone if lead_obj else ""),
            "duration_seconds": dur,
            "formatted_duration": fmt_dur,
            "date": to_ist_datetime_str(call_obj.created_at, "%Y-%m-%d %H:%M"),
        })

        date_str = call_date.strftime("%Y-%m-%d")
        if date_str not in date_map:
            date_map[date_str] = {"total_seconds": 0, "count": 0}
        date_map[date_str]["total_seconds"] += dur
        date_map[date_str]["count"] += 1

    date_history = []
    for d_str, val in sorted(date_map.items(), reverse=True):
        d_dur = val["total_seconds"]
        d_min = d_dur // 60
        d_sec = d_dur % 60
        date_history.append({
            "date": d_str,
            "total_seconds": d_dur,
            "formatted_duration": f"{d_min:02d}:{d_sec:02d}",
            "call_count": val["count"],
        })

    tot_min = total_seconds // 60
    tot_sec = total_seconds % 60
    tod_min = today_seconds // 60
    tod_sec = today_seconds % 60

    return {
        "total_duration_seconds": total_seconds,
        "formatted_total_time": f"{tot_min:02d}:{tot_sec:02d}",
        "today_duration_seconds": today_seconds,
        "formatted_today_time": f"{tod_min:02d}:{tod_sec:02d}",
        "individual_calls": individual_calls,
        "date_wise_history": date_history,
    }


# ---------------------------------------------------------------------------
# 4. Agent Status Management
# ---------------------------------------------------------------------------

@router.post("/agent/status")
async def update_agent_status(
    payload: AgentStatusUpdate,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    valid_statuses = ["AVAILABLE", "BUSY", "ON CALL", "PAUSED", "OFFLINE", "ON BREAK", "COFFEE_BREAK", "LUNCH_BREAK"]
    if payload.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Invalid status. Must be one of {valid_statuses}")

    old_status = user.status
    break_statuses = ["COFFEE_BREAK", "LUNCH_BREAK", "PAUSED", "ON BREAK"]

    # Enforce single active break: if already on a break and trying to start a DIFFERENT break without resuming
    if old_status in break_statuses and payload.status in break_statuses and old_status != payload.status:
        raise HTTPException(
            status_code=400, 
            detail="An active break is already in progress. Please resume or end your current break before starting a new one."
        )

    # If status is already identical, return current status without duplicate audit log
    if old_status == payload.status:
        return {"status": user.status, "message": f"Agent status is already {user.status}"}

    user.status = payload.status
    db.add(user)

    # Sync to Expert if linked
    stmt_expert = select(Expert).where(Expert.user_id == user.id)
    expert = (await db.execute(stmt_expert)).scalar_one_or_none()
    if expert:
        expert.status = payload.status
        db.add(expert)

    from app.db.models import AuditLog
    audit = AuditLog(
        user_id=user.id,
        business_id=user.business_id,
        action="AGENT_STATUS_CHANGED",
        entity="User",
        entity_id=user.id,
        details={"from_status": old_status, "to_status": payload.status}
    )
    db.add(audit)

    await db.commit()
    await db.refresh(user)
    return {"status": user.status, "message": f"Agent status updated to {user.status}"}


# ---------------------------------------------------------------------------
# 5. Calls in Queue Detailed View
# ---------------------------------------------------------------------------

@router.get("/queue")
async def get_calls_in_queue(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    expert_id = expert.id if expert else user.id
    agent_ids = list(set(filter(None, [user.id, expert_id])))

    user_biz = user.business_id
    if not user_biz and (user.role or "").upper() != "SUPER_ADMIN":
        return []

    stmt = (
        select(AgentAssignmentQueue, Lead, Campaign)
        .outerjoin(Lead, AgentAssignmentQueue.lead_id == Lead.id)
        .outerjoin(Campaign, AgentAssignmentQueue.campaign_id == Campaign.id)
        .where(
            and_(
                AgentAssignmentQueue.business_id == user_biz if user_biz else True,
                or_(
                    AgentAssignmentQueue.requested_expert_id.in_(agent_ids),
                    AgentAssignmentQueue.assigned_agent_id.in_(agent_ids),
                    AgentAssignmentQueue.requested_expert_id.is_(None),
                ),
                AgentAssignmentQueue.status == "WAITING"
            )
        )
        .order_by(AgentAssignmentQueue.created_at.asc())
    )
    rows = (await db.execute(stmt)).all()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    queue_list = []

    for idx, row in enumerate(rows, start=1):
        q_item: AgentAssignmentQueue = row[0]
        lead_item: Optional[Lead] = row[1]
        campaign_item: Optional[Campaign] = row[2]

        waiting_sec = int((now - q_item.created_at).total_seconds())
        m = waiting_sec // 60
        s = waiting_sec % 60
        fmt_waiting = f"{m:02d}:{s:02d}"

        customer_name = f"{lead_item.first_name} {lead_item.last_name}".strip() if lead_item else "Customer"
        if not customer_name:
            customer_name = "Customer"

        queue_list.append({
            "id": q_item.id,
            "position": idx,
            "customer_name": customer_name,
            "customer_phone": lead_item.phone if lead_item else "",
            "lead_details": {
                "id": lead_item.id if lead_item else "",
                "company": lead_item.company if lead_item else "—",
                "requirement": lead_item.requirement if lead_item else "—",
            },
            "campaign_name": campaign_item.name if campaign_item else "",
            "assigned_agent": user.full_name or "",
            "assigned_status": "Assigned",
            "waiting_time_seconds": waiting_sec,
            "formatted_waiting_time": fmt_waiting,
            "queue_status": q_item.status,
            "priority": "NORMAL",
            "date_time_added": q_item.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        })

    return queue_list


# ---------------------------------------------------------------------------
# 6. Calls on Hold Detailed View
# ---------------------------------------------------------------------------

@router.get("/on-hold")
async def get_calls_on_hold(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    expert_id = expert.id if expert else user.id
    agent_ids = list(set(filter(None, [user.id, expert_id])))

    user_biz = user.business_id
    if not user_biz and (user.role or "").upper() != "SUPER_ADMIN":
        return []

    stmt = (
        select(ParkedCall, Call, Lead)
        .join(Call, ParkedCall.call_id == Call.id)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .where(
            and_(
                ParkedCall.business_id == user_biz if user_biz else True,
                or_(ParkedCall.agent_id.in_(agent_ids), Call.agent_id.in_(agent_ids), Call.user_id == user.id),
                ParkedCall.status == "ON_HOLD"
            )
        )
        .order_by(desc(ParkedCall.hold_start_time))
    )
    rows = (await db.execute(stmt)).all()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    held_calls = []

    for row in rows:
        parked: ParkedCall = row[0]
        call: Call = row[1]
        lead: Optional[Lead] = row[2]

        hold_sec = int((now - parked.hold_start_time).total_seconds())
        m = hold_sec // 60
        s = hold_sec % 60

        customer_name = f"{lead.first_name} {lead.last_name}".strip() if lead else "Customer"

        held_calls.append({
            "id": parked.id,
            "call_id": call.id,
            "customer_name": customer_name or "Unknown Customer",
            "customer_number": call.to_number or (lead.phone if lead else ""),
            "agent_name": user.full_name or "Agent",
            "campaign_name": "",
            "hold_start_time": parked.hold_start_time.strftime("%I:%M %p"),
            "time_on_hold_seconds": hold_sec,
            "formatted_time_on_hold": f"{m:02d}:{s:02d}",
            "hold_reason": parked.hold_reason or "PARKED",
            "call_status": "ON_HOLD",
            "slot": parked.slot or "D1",
        })

    return held_calls


# ---------------------------------------------------------------------------
# 7. Client Configuration Management (D1 / D2 / D3)
# ---------------------------------------------------------------------------

@router.get("/client-configs")
async def get_client_configs(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    biz_id = user.business_id
    if not biz_id:
        return []

    stmt = select(ClientConfig).where(ClientConfig.business_id == biz_id).order_by(ClientConfig.slot.asc())
    configs = (await db.execute(stmt)).scalars().all()

    # Enrich client details if client_id exists
    result = []
    for cfg in configs:
        c_name = cfg.client_name
        c_phone = cfg.client_phone
        if cfg.client_id:
            c_obj = await db.get(Client, cfg.client_id)
            if c_obj:
                c_name = c_obj.name
                c_phone = c_obj.contact_number
        result.append({
            "id": cfg.id,
            "business_id": cfg.business_id,
            "slot": cfg.slot,
            "client_id": cfg.client_id,
            "client_name": c_name,
            "client_phone": c_phone,
        })
    return result


@router.get("/client-configs/slot-mappings")
async def get_slot_mappings(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    biz_id = user.business_id
    mappings = {"D1": "", "D2": "", "D3": ""}
    if not biz_id:
        return mappings

    stmt = select(ClientConfig).where(ClientConfig.business_id == biz_id)
    configs = (await db.execute(stmt)).scalars().all()
    for cfg in configs:
        if cfg.slot in mappings:
            mappings[cfg.slot] = cfg.client_id or ""

    return mappings


@router.post("/client-configs")
async def create_or_update_client_config(
    payload: ClientConfigIn,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    if payload.slot not in ("D1", "D2", "D3"):
        raise HTTPException(status_code=400, detail="Slot must be D1, D2, or D3.")

    biz_id = user.business_id
    if not biz_id:
        from app.db.models import Business
        biz = await db.scalar(select(Business))
        biz_id = biz.id if biz else None
    if not biz_id:
        raise HTTPException(status_code=400, detail="User is not associated with a business.")

    client_obj = None
    if payload.client_id:
        client_obj = await db.get(Client, payload.client_id)

    client_name = (client_obj.name if client_obj else payload.client_name or "").strip()
    client_phone = (client_obj.contact_number if client_obj else payload.client_phone or "").strip()

    stmt = select(ClientConfig).where(
        and_(ClientConfig.business_id == biz_id, ClientConfig.slot == payload.slot)
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing:
        existing.client_id = payload.client_id if payload.client_id else None
        existing.client_name = client_name
        existing.client_phone = client_phone
        db.add(existing)
        target = existing
    else:
        target = ClientConfig(
            business_id=biz_id,
            slot=payload.slot,
            client_id=payload.client_id if payload.client_id else None,
            client_name=client_name,
            client_phone=client_phone,
        )
        db.add(target)

    await db.commit()
    await db.refresh(target)
    return {
        "id": target.id,
        "business_id": target.business_id,
        "slot": target.slot,
        "client_id": target.client_id,
        "client_name": target.client_name,
        "client_phone": target.client_phone,
    }


@router.post("/client-configs/batch")
async def save_batch_slot_mappings(
    payload: BatchClientConfigIn,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    biz_id = user.business_id
    if not biz_id:
        from app.db.models import Business
        biz = await db.scalar(select(Business))
        biz_id = biz.id if biz else None
    if not biz_id:
        raise HTTPException(status_code=400, detail="User is not associated with a business.")

    input_mappings = payload.slot_mappings or {}
    if payload.D1 is not None: input_mappings["D1"] = payload.D1
    if payload.D2 is not None: input_mappings["D2"] = payload.D2
    if payload.D3 is not None: input_mappings["D3"] = payload.D3

    result_mappings = {"D1": "", "D2": "", "D3": ""}

    for slot_key in ("D1", "D2", "D3"):
        c_id = input_mappings.get(slot_key)
        client_obj = await db.get(Client, c_id) if c_id else None

        stmt = select(ClientConfig).where(
            and_(ClientConfig.business_id == biz_id, ClientConfig.slot == slot_key)
        )
        existing = (await db.execute(stmt)).scalar_one_or_none()

        if client_obj:
            c_name = client_obj.name
            c_phone = client_obj.contact_number
            c_id_val = client_obj.id
        else:
            c_name = ""
            c_phone = ""
            c_id_val = None

        if existing:
            existing.client_id = c_id_val
            existing.client_name = c_name
            existing.client_phone = c_phone
            db.add(existing)
        else:
            new_cfg = ClientConfig(
                business_id=biz_id,
                slot=slot_key,
                client_id=c_id_val,
                client_name=c_name,
                client_phone=c_phone,
            )
            db.add(new_cfg)

        result_mappings[slot_key] = c_id_val or ""

    await db.commit()
    return result_mappings


@router.delete("/client-configs/{slot}")
async def delete_client_config(
    slot: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    if not user.business_id:
        raise HTTPException(status_code=400, detail="User is not associated with a business.")

    stmt = select(ClientConfig).where(
        and_(ClientConfig.business_id == user.business_id, ClientConfig.slot == slot)
    )
    config = (await db.execute(stmt)).scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail=f"Client config for slot {slot} not found.")

    await db.delete(config)
    await db.commit()
    return {"message": f"Client config for slot {slot} deleted."}


# ---------------------------------------------------------------------------
# 7.5 Parked Call Conference Participants (D1 / D2 / D3)
# ---------------------------------------------------------------------------

@router.post("/parked-call/{call_id}/conference")
async def save_parked_conference(
    call_id: str,
    payload: ParkedConferenceRequest,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ParkedConference).where(ParkedConference.call_id == call_id)
    conf = (await db.execute(stmt)).scalar_one_or_none()
    
    d1 = (payload.D1 or "").strip()
    d2 = (payload.D2 or "").strip()
    d3 = (payload.D3 or "").strip()
    
    if conf:
        conf.D1_phone = d1
        conf.D2_phone = d2
        conf.D3_phone = d3
        db.add(conf)
    else:
        conf = ParkedConference(
            call_id=call_id,
            D1_phone=d1,
            D2_phone=d2,
            D3_phone=d3,
        )
        db.add(conf)
        
    await db.commit()
    await db.refresh(conf)
    
    return {
        "D1": conf.D1_phone,
        "D2": conf.D2_phone,
        "D3": conf.D3_phone,
    }


@router.get("/parked-call/{call_id}/conference")
async def get_parked_conference(
    call_id: str,
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ParkedConference).where(ParkedConference.call_id == call_id)
    conf = (await db.execute(stmt)).scalar_one_or_none()
    
    if conf:
        return {
            "D1": conf.D1_phone,
            "D2": conf.D2_phone,
            "D3": conf.D3_phone,
        }
    return {
        "D1": "",
        "D2": "",
        "D3": "",
    }


# ---------------------------------------------------------------------------
# 7.8 Real-time Call Initiation & Active Call Recovery
# ---------------------------------------------------------------------------

def _to_utc_iso(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


async def _resolve_ai_agent_id(db: AsyncSession, biz_id: str | None, preferred_agent_id: str | None = None) -> str | None:
    from app.db.models import AIAgent
    if preferred_agent_id:
        agent = await db.get(AIAgent, preferred_agent_id)
        if agent and (not biz_id or agent.business_id == biz_id):
            return agent.id
    if biz_id:
        agent_row = (await db.execute(select(AIAgent).where(AIAgent.business_id == biz_id))).scalars().first()
        if agent_row:
            return agent_row.id
    return None


@router.post("/calls/initiate")
async def initiate_call(
    payload: InitiateCallRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business, Campaign, AIAgent
    from app.api.routes.calls import normalize_to_e164
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call_id = f"call-{uuid.uuid4().hex[:12]}"

    # Deduplicate rapid repeated clicks / requests within 3 seconds for the same destination
    if payload.phone_number:
        clean_num_check = normalize_to_e164(payload.phone_number) or payload.phone_number.strip()
        recent_call_stmt = select(Call).where(
            Call.user_id == user.id,
            Call.to_number == clean_num_check,
            Call.status.in_(["RINGING", "CONNECTED", "HUMAN_HANDLING"]),
            Call.initiated_at >= (now - timedelta(seconds=3))
        ).order_by(Call.initiated_at.desc())
        recent_call = (await db.execute(recent_call_stmt)).scalars().first()
        if recent_call:
            logger.info(f"[OUTBOUND_DIAL_DEDUP] Duplicate dial attempt within 3s for {clean_num_check}. Returning active call {recent_call.id}.")
            return {
                "call_id": recent_call.id,
                "status": recent_call.status,
                "initiated_at": to_utc_iso(recent_call.initiated_at),
                "connected_at": to_utc_iso(recent_call.connected_at) if recent_call.connected_at else None,
                "lead_id": recent_call.lead_id,
                "lead_name": recent_call.client_name or "Customer",
                "phone": recent_call.to_number,
            }

    lead = await db.get(Lead, payload.lead_id) if payload.lead_id else None
    
    biz_id = user.business_id or (lead.business_id if lead else None)
    if not biz_id:
        if (user.role or "").upper() == "SUPER_ADMIN":
            from app.core.deps import PRIMARY_BUSINESS_ID
            biz_id = PRIMARY_BUSINESS_ID
        else:
            raise HTTPException(status_code=400, detail="Cannot initiate call: Tenant workspace not configured for user.")

    # Resolve or create Lead if caller dialed a number directly without lead_id or dialed a different number
    raw_dest = (payload.phone_number or (lead.phone if lead else "")).strip()
    if lead and payload.phone_number:
        clean_raw_dest = "".join(c for c in str(payload.phone_number) if c.isdigit())
        clean_lead_phone = "".join(c for c in str(lead.phone or "") if c.isdigit())
        if clean_raw_dest and clean_lead_phone and not (clean_raw_dest.endswith(clean_lead_phone[-10:]) or clean_lead_phone.endswith(clean_raw_dest[-10:])):
            logger.info(f"[OUTBOUND_DIAL] Dialed number {payload.phone_number} does not match lead {lead.id} phone {lead.phone}. Resolving dialed number independently.")
            lead = None

    if not lead and raw_dest:
        clean_num = raw_dest
        lead_stmt = select(Lead).where(
            or_(
                Lead.phone == clean_num,
                Lead.phone == f"+{clean_num}",
                Lead.phone.like(f"%{clean_num.lstrip('+0')}%")
            )
        )
        if biz_id:
            lead_stmt = lead_stmt.where(Lead.business_id == biz_id)
        lead = (await db.execute(lead_stmt)).scalars().first()
        if not lead:
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                phone=clean_num,
                first_name=payload.client_name or "Prospect",
                source="OUTBOUND_DIAL",
                status="CALLING",
            )
            db.add(lead)
            await db.flush()

    # Validate campaign_id if supplied
    valid_campaign_id = None
    campaign_obj = None
    if payload.campaign_id:
        campaign_obj = await db.get(Campaign, payload.campaign_id)
        if campaign_obj:
            valid_campaign_id = campaign_obj.id



    # Determine whether this call is assigned to an AI Agent or a Voice Agent
    resolved_agent_type: str = "HUMAN"
    resolved_agent_id: Optional[str] = None
    decision_reason: str = ""
    camp_primary_type: str = "NONE"
    transfer_agent_ids: str = "NONE"

    if campaign_obj:
        camp_primary_type = (campaign_obj.agent_type or "AI_VOICE").upper()
        transfer_agent_ids = campaign_obj.voice_agent_id or "NONE"

        # Enforce isolation: Campaigns created for a Voice Agent must not be accessible to or dialed by the AI Agent
        is_voice_agent_camp = (
            camp_primary_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
            or "HUMAN" in camp_primary_type
            or (campaign_obj.voice_agent_id and not campaign_obj.agent_id)
        )
        if is_voice_agent_camp and payload.agent_type and str(payload.agent_type).strip().upper() in ("AI", "AI_VOICE", "AI_AGENT"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Campaigns created for a Voice Agent must not be accessible to or dialed by the AI Agent."
            )

    # Lead assigned to Voice Agent isolation: must not be dialed by AI Agent
    if lead and lead.assigned_expert_id:
        if payload.agent_type and str(payload.agent_type).strip().upper() in ("AI", "AI_VOICE", "AI_AGENT"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This lead is assigned to a Voice Agent and cannot be dialed by an AI Agent."
            )

    # Priority 1: Explicit human or AI override in payload
    if payload.agent_type:
        p_type = str(payload.agent_type).strip().upper()
        if p_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN") or "HUMAN" in p_type:
            resolved_agent_type = "HUMAN"
            resolved_agent_id = payload.agent_id or (campaign_obj.voice_agent_id if campaign_obj else None) or user.id
            decision_reason = "payload_explicit_human"
        elif p_type in ("AI", "AI_VOICE", "AI_AGENT"):
            resolved_agent_type = "AI_VOICE"
            resolved_agent_id = payload.agent_id or (campaign_obj.agent_id if campaign_obj else None)
            decision_reason = "payload_explicit_ai"

    # Priority 2: Campaign assignment
    if resolved_agent_id is None and campaign_obj:
        if (
            camp_primary_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
            or "HUMAN" in camp_primary_type
        ):
            resolved_agent_type = "HUMAN"
            resolved_agent_id = campaign_obj.voice_agent_id or user.id
            decision_reason = "campaign_configured_as_human"
        elif getattr(user, "role", None) in ("VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE"):
            # The logged-in user is a human Voice Agent dialing on an AI campaign from dashboard
            resolved_agent_type = "HUMAN"
            resolved_agent_id = payload.agent_id or campaign_obj.voice_agent_id or user.id
            decision_reason = "voice_agent_user_role_human"
        else:
            # AI Agent campaign dialed by non-voice-agent or automated caller
            resolved_agent_type = "AI_VOICE"
            resolved_agent_id = campaign_obj.agent_id
            decision_reason = "campaign_configured_as_ai"

    # Priority 3: Non-campaign explicit agent_id in payload
    if resolved_agent_id is None and payload.agent_id:
        c_ai = await db.get(AIAgent, payload.agent_id)

        if c_ai:
            resolved_agent_type = "AI_VOICE"
            resolved_agent_id = c_ai.id
            decision_reason = "payload_ai_agent_found"
        else:
            # Check if payload.agent_id is a valid human Expert or User
            expert_obj = await db.get(Expert, payload.agent_id)
            if not expert_obj:
                expert_obj = await db.get(User, payload.agent_id)
            if expert_obj:
                resolved_agent_type = "HUMAN"
                resolved_agent_id = payload.agent_id
                decision_reason = "payload_expert_found"
            else:
                logger.warning(
                    f"[OUTBOUND_DIAL] Invalid AI agent_id={payload.agent_id}"
                )
                raise HTTPException(
                    status_code=400,
                    detail="Invalid AI agent ID"
                )

    # Priority 4: Lead assignment if lead has assigned expert or agent (for non-campaign calls)
    if resolved_agent_id is None and lead:
        if lead.assigned_expert_id:
            resolved_agent_type = "HUMAN"
            resolved_agent_id = lead.assigned_expert_id
            decision_reason = "lead_assigned_to_expert"
        elif lead.agent_id:
            resolved_agent_type = "AI_VOICE"
            resolved_agent_id = lead.agent_id
            decision_reason = "lead_assigned_to_ai"

    # Priority 5: Default for Voice Agent Dashboard manual dial
    if resolved_agent_id is None:
        resolved_agent_type = "HUMAN"
        resolved_agent_id = user.id
        decision_reason = "dashboard_manual_default_human"

    # If AI Agent assigned, ensure valid AIAgent id is resolved
    if resolved_agent_type == "AI_VOICE":
        if not resolved_agent_id:
            ai_agent = await db.scalar(select(AIAgent).where(AIAgent.status == "ACTIVE"))
            if not ai_agent:
                ai_agent = await db.scalar(select(AIAgent).order_by(AIAgent.created_at.asc()))
            resolved_agent_id = ai_agent.id if ai_agent else await _resolve_ai_agent_id(db, biz_id, user.id)

    from app.api.routes.calls import normalize_to_e164
    from app.services.voice_service import get_voice_provider, is_mock_mode

    raw_dest = payload.phone_number or (lead.phone if lead else "")
    lead_phone = normalize_to_e164(raw_dest) or raw_dest
    lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else "Customer"

    # Enforce strict backend DND suppression check before placing call or creating call record
    if lead_phone:
        from app.services.dnd_service import is_number_suppressed
        if await is_number_suppressed(db, lead_phone, biz_id):
            logger.warning(f"[OUTBOUND_CALL_SUPPRESSED] Phone '{lead_phone}' is on DND suppression list. Blocking initiate_call.")
            if lead:
                lead.status = "DNC"
                lead.call_status = "DNC"
                lead.call_result = "Do Not Call (DNC)"
                db.add(lead)
                await db.commit()
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Outbound call blocked: Phone number {lead_phone} is registered on the Do Not Call (DND) suppression list."
            )

    caller_id = (
        (campaign_obj.twilio_number if campaign_obj and campaign_obj.twilio_number else None)
        or getattr(settings, "sip_username", None)
        or getattr(settings, "sip_did_number", None)
        or settings.twilio_phone_number
        or ""
    )

    from app.db.models import Conversation
    conversation = None
    if biz_id and lead and lead.id:
        try:
            async with db.begin_nested():
                conv_agent_id = None
                if resolved_agent_id:
                    c_agent = await db.get(AIAgent, resolved_agent_id)
                    if c_agent:
                        conv_agent_id = c_agent.id

                conversation = Conversation(
                    id=str(uuid.uuid4()),
                    business_id=biz_id,
                    lead_id=lead.id,
                    agent_id=conv_agent_id,
                    channel="VOICE",
                    status="ACTIVE",
                )
                db.add(conversation)
                await db.flush()
        except Exception as conv_err:
            logger.warning(f"[OUTBOUND_DIAL] Conversation record creation notice: {conv_err}")
            conversation = None

    call = Call(
        id=call_id,
        business_id=biz_id,
        lead_id=lead.id if lead else None,
        campaign_id=valid_campaign_id,
        agent_id=resolved_agent_id,
        user_id=user.id,
        conversation_id=conversation.id if conversation else None,
        from_number=caller_id,
        to_number=lead_phone,
        status="RINGING",
        direction="OUTBOUND",
        agent_type=resolved_agent_type,
        client_name=payload.client_name or lead_name or "",
        client_phone=payload.client_phone or lead_phone or "",
        initiated_at=now,
        connected_at=None,
        ended_at=None,
    )
    db.add(call)

    # Tag barge session for outbound dashboard call
    try:
        from app.api.routes.barge_stream import _get_or_create_session
        sess = await _get_or_create_session(call.id)
        sess["is_dashboard_outbound"] = True
        sess["agent_type"] = resolved_agent_type
        sess["is_voice_agent_call"] = (resolved_agent_type == "HUMAN")
        sess["ai_bot_started"] = False
        sess["ai_speaking"] = False
        if resolved_agent_type == "HUMAN":
            ai_q = sess.get("ai_rtp_tx_queue")
            if ai_q is not None and len(ai_q) > 0:
                ai_q.clear()
    except Exception as exc:
        logger.debug(f"Barge session tag note: {exc}")

    logger.info(
        f"[CALL_ROUTING] call_id={call.id} direction=OUTBOUND "
        f"campaign_id={valid_campaign_id or 'NONE'} campaign_primary_agent_type={camp_primary_type} "
        f"primary_agent_id={resolved_agent_id} transfer_agent_ids={transfer_agent_ids} "
        f"resolved_agent_type={resolved_agent_type} decision_reason='{decision_reason}' "
        f"ai_bot={'WILL_START' if resolved_agent_type == 'AI_VOICE' else 'SKIPPED'}"
    )
    
    try:
        user.status = "ON CALL"
        db.add(user)
    except Exception as u_err:
        logger.debug(f"User status update note: {u_err}")
    
    if lead:
        try:
            # assigned_expert_id has ForeignKey("experts.id") - never assign user.id directly without expert lookup
            exp_match = await db.scalar(select(Expert).where(Expert.user_id == user.id))
            if exp_match:
                lead.assigned_expert_id = exp_match.id
            lead.call_status = "RINGING"
            lead.status = "CALLING"
            db.add(lead)
        except Exception as l_err:
            logger.debug(f"Lead status update note: {l_err}")

    # Register participants
    try:
        async with db.begin_nested():
            agent_part = CallParticipant(
                id=f"part-{uuid.uuid4().hex[:12]}",
                call_id=call.id,
                participant_type="AGENT",
                participant_id=user.id,
                phone_number="",
                joined_at=now,
                status="RINGING",
            )
            cust_part = CallParticipant(
                id=f"part-{uuid.uuid4().hex[:12]}",
                call_id=call.id,
                participant_type="CUSTOMER",
                participant_id=None,
                phone_number=lead_phone,
                joined_at=now,
                status="RINGING",
            )
            db.add_all([agent_part, cust_part])
            await db.flush()
    except Exception as p_err:
        logger.debug(f"Participants registration note: {p_err}")

    try:
        await db.commit()
        await db.refresh(call)
    except Exception as commit_err:
        logger.error(f"[OUTBOUND_DIAL] Initial commit failed: {commit_err}")
        await db.rollback()
        try:
            call.conversation_id = None
            if lead:
                db.add(lead)
            db.add(call)
            await db.commit()
            await db.refresh(call)
        except Exception as retry_err:
            logger.error(f"[OUTBOUND_DIAL] Retry commit failed: {retry_err}")
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Failed to record call: {retry_err}")

    # Real Telephony Provider call placement (PSTN via Twilio / SIP)
    assigned_sip_acc = None
    if getattr(payload, "sip_account_id", None):
        from app.services.sip_account_service import validate_sip_account_for_campaign
        is_val, err_m, assigned_sip_acc = await validate_sip_account_for_campaign(
            db, payload.sip_account_id, biz_id, call_direction="outbound"
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
    elif valid_campaign_id:
        camp_obj = await db.get(Campaign, valid_campaign_id)
        if camp_obj and getattr(camp_obj, "sip_account_id", None):
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
            selected_did = None
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
                        selected_did = did_row.phone_number.strip()
            if not selected_did and assigned_sip_acc.caller_id:
                selected_did = assigned_sip_acc.caller_id.strip()
            if not selected_did and getattr(camp_obj, "twilio_number", None) and camp_obj.twilio_number != "LOCAL_TEST":
                selected_did = camp_obj.twilio_number.strip()
            if selected_did:
                caller_id = selected_did
            call.from_number = caller_id
            db.add(call)
            await db.commit()

    # If no SIP account assigned via campaign (e.g. manual dial pad or campaign without explicit sip_account_id),
    # retrieve active outbound SIP account strictly within the current tenant/organization
    if not assigned_sip_acc and biz_id:
        from app.db.models import SIPAccount
        sip_stmt_biz = select(SIPAccount).where(
            SIPAccount.organization_id == biz_id,
            SIPAccount.enabled == True,
            func.lower(SIPAccount.direction).in_(["outbound", "both", "bidirectional"]),
        )
        assigned_sip_acc = (await db.execute(sip_stmt_biz)).scalars().first()
        if assigned_sip_acc:
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

    # Verify SIP account connectivity / registration if required
    if assigned_sip_acc and str(assigned_sip_acc.registration_status).lower() not in ("registered", "active", "healthy", "peer_active", "outbound_ready"):
        from app.services.sip_account_service import test_and_update_sip_account
        try:
            await test_and_update_sip_account(db, assigned_sip_acc)
        except Exception as probe_err:
            logger.warning(f"[SIP_ACCOUNT_PROBE] Connectivity probe notice: {probe_err}")

    provider = get_voice_provider()
    public_base = settings.public_base_url.rstrip("/")
    answer_url = f"{public_base}/api/voice/answer?call_id={call.id}"
    status_url = f"{public_base}/api/voice/status?call_id={call.id}"

    try:
        logger.info(f"[OUTBOUND_DIAL] Placing live call to {lead_phone} from {caller_id} via {provider.__class__.__name__} (sip_account={getattr(assigned_sip_acc, 'name', None)})")
        sid = await provider.place_call(
            to=lead_phone,
            from_=caller_id,
            answer_url=answer_url,
            status_callback_url=status_url,
            sip_account=assigned_sip_acc,
        )
        call.call_sid = sid
        if lead:
            lead.twilio_call_sid = sid
            db.add(lead)
        db.add(call)
        await db.commit()
        logger.info(f"[OUTBOUND_DIAL_PLACED] Call SID: {sid}")
        if sid:
            try:
                from app.api.routes.barge_stream import barge_sessions
                b_s = barge_sessions.get(call.id)
                if b_s:
                    barge_sessions[sid] = b_s
            except Exception:
                pass
    except Exception as tel_err:
        logger.warning(f"[OUTBOUND_DIAL_NOTICE] Telephony provider notification: {tel_err}")
    
    utc_now_iso = to_utc_iso(now)
    from app.api.routes.websockets import global_ws_manager
    try:
        await global_ws_manager.broadcast(json.dumps({
            "event": "CALL_RINGING",
            "call_id": call.id,
            "lead_id": call.lead_id,
            "lead_name": lead_name,
            "status": "RINGING",
            "initiated_at": utc_now_iso,
            "connected_at": None,
        }))
    except Exception as exc:
        logger.debug(f"WS broadcast note: {exc}")

    return {
        "call_id": call.id,
        "status": "RINGING",
        "initiated_at": utc_now_iso,
        "connected_at": None,
        "lead_id": call.lead_id,
        "lead_name": lead_name,
        "phone": lead_phone,
    }


@router.post("/calls/{call_id}/answered")
async def mark_call_answered(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Called when the customer answers the phone or live connection is established."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call.status = "HUMAN_HANDLING"
    call.connected_at = now
    db.add(call)

    if call.lead_id:
        lead = await db.get(Lead, call.lead_id)
        if lead:
            lead.call_status = "CONNECTED"
            db.add(lead)

    await db.commit()
    await db.refresh(call)

    utc_iso = to_utc_iso(now)
    from app.api.routes.websockets import global_ws_manager
    try:
        await global_ws_manager.broadcast(json.dumps({
            "event": "CALL_CONNECTED",
            "call_id": call.id,
            "lead_id": call.lead_id,
            "status": "HUMAN_HANDLING",
            "connected_at": utc_iso,
        }))
    except Exception as exc:
        logger.debug(f"WS broadcast note: {exc}")

    return {"success": True, "status": "HUMAN_HANDLING", "connected_at": utc_iso}


@router.post("/calls/{call_id}/hold")
async def put_call_on_hold(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Puts customer call on hold and streams hold music during agent transfer."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    call.status = "ON_HOLD"
    db.add(call)
    await db.commit()
    await db.refresh(call)

    from app.services.hold_music_service import start_customer_hold
    try:
        await start_customer_hold(db, call)
    except Exception as exc:
        logger.debug(f"Hold music trigger notice: {exc}")

    hold_url = getattr(settings, "hold_music_url", "https://s3.amazonaws.com/com.twilio.music.classical/MARKOVICHAMP-Borghestral.mp3")
    from app.api.routes.websockets import global_ws_manager
    try:
        await global_ws_manager.broadcast(json.dumps({
            "event": "CALL_ON_HOLD",
            "call_id": call.id,
            "status": "ON_HOLD",
            "hold_music_url": hold_url,
            "message": "Call placed on hold for transfer.",
        }))
    except Exception:
        pass

    return {
        "success": True,
        "status": "ON_HOLD",
        "hold_music_url": hold_url,
    }


@router.post("/calls/{call_id}/resume")
async def resume_call_from_hold(
    call_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Resumes call from hold."""
    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    call.status = "HUMAN_HANDLING"
    db.add(call)
    await db.commit()

    from app.services.hold_music_service import stop_customer_hold
    try:
        await stop_customer_hold(db, call)
    except Exception:
        pass

    from app.api.routes.websockets import global_ws_manager
    try:
        await global_ws_manager.broadcast(json.dumps({
            "event": "CALL_CONNECTED",
            "call_id": call.id,
            "status": "HUMAN_HANDLING",
        }))
    except Exception:
        pass

    return {"success": True, "status": "HUMAN_HANDLING"}


@router.get("/active-call")
async def get_active_call_session(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = (
        select(Call, Lead)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .where(
            and_(
                or_(Call.user_id == user.id, Call.agent_id == user.id),
                Call.status.in_([
                    "INITIATING", "RINGING", "IN_PROGRESS", "AI_HANDLING",
                    "TRANSFER_REQUESTED", "HUMAN_HANDLING", "CLIENT_ADDING",
                    "CONFERENCE_3WAY_ACTIVE", "ON_HOLD"
                ])
            )
        )
        .order_by(desc(Call.created_at))
    )
    res = (await db.execute(stmt)).first()
    if not res:
        if user.status == "ON CALL":
            user.status = "AVAILABLE"
            db.add(user)
            await db.commit()
        return {
            "has_active_call": False,
            "caller_number": settings.twilio_phone_number or "",
            "from_number": settings.twilio_phone_number or "",
        }
        
    call_obj: Call = res[0]
    lead_obj: Optional[Lead] = res[1]

    # Stale/abandoned call check: if unended call is older than 90s, auto-complete it cleanly
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    base_t = call_obj.connected_at or call_obj.initiated_at or call_obj.created_at
    if base_t and (now_utc - base_t).total_seconds() > 90:
        call_obj.status = "COMPLETED"
        call_obj.ended_at = now_utc
        call_obj.duration_seconds = max(1, int((now_utc - base_t).total_seconds())) if (now_utc - base_t).total_seconds() < 300 else 60
        db.add(call_obj)
        if user.status == "ON CALL":
            user.status = "AVAILABLE"
            db.add(user)
        await db.commit()
        return {
            "has_active_call": False,
            "caller_number": settings.twilio_phone_number or "",
            "from_number": settings.twilio_phone_number or "",
        }
    
    customer_name = f"{lead_obj.first_name} {lead_obj.last_name}".strip() if lead_obj else (call_obj.client_name or "Customer")
    customer_phone = call_obj.to_number or (lead_obj.phone if lead_obj else "")
    caller_phone = call_obj.from_number or settings.twilio_phone_number or ""
    
    stmt_park = select(ParkedCall).where(and_(ParkedCall.call_id == call_obj.id, ParkedCall.status == "ON_HOLD"))
    parked = (await db.execute(stmt_park)).scalars().first()
    
    return {
        "has_active_call": True,
        "call_id": call_obj.id,
        "lead_id": call_obj.lead_id,
        "lead_name": customer_name,
        "lead_phone": customer_phone,
        "caller_number": caller_phone,
        "from_number": caller_phone,
        "campaign_id": call_obj.campaign_id,
        "status": call_obj.status,
        "direction": call_obj.direction,
        "initiated_at": _to_utc_iso(call_obj.initiated_at or call_obj.created_at),
        "connected_at": _to_utc_iso(call_obj.connected_at),
        "ended_at": _to_utc_iso(call_obj.ended_at),
        "client_connected_at": _to_utc_iso(call_obj.client_connected_at),
        "client_disconnected_at": _to_utc_iso(call_obj.client_disconnected_at),
        "client_name": call_obj.client_name,
        "client_phone": call_obj.client_phone,
        "client_duration_seconds": call_obj.client_duration_seconds or 0,
        "is_parked": parked is not None or call_obj.status == "ON_HOLD",
        "slot": parked.slot if parked else "D1",
    }


# ---------------------------------------------------------------------------
# 8. Park Call & Resume
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/park")
async def park_call(
    id: str,
    payload: ParkCallRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call = await db.get(Call, id)
    if not call:
        biz_id = user.business_id
        if not biz_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            biz_id = biz_row.id if biz_row else "default-biz"
        call = Call(
            id=id,
            business_id=biz_id,
            agent_id=await _resolve_ai_agent_id(db, biz_id, user.id),
            from_number=settings.twilio_phone_number or "",
            to_number=settings.twilio_phone_number or "",
            status="ON_HOLD",
            direction="OUTBOUND",
            initiated_at=now,
            connected_at=now,
        )
        db.add(call)
    else:
        call.status = "ON_HOLD"
        db.add(call)

    slot_name = payload.slot or "D1"
    stmt_existing = select(ParkedCall).where(and_(ParkedCall.call_id == call.id, ParkedCall.status == "ON_HOLD"))
    existing_parked = (await db.execute(stmt_existing)).scalars().first()
    if existing_parked:
        existing_parked.slot = slot_name
        existing_parked.hold_reason = payload.hold_reason or "PARKED"
        db.add(existing_parked)
        parked = existing_parked
    else:
        biz_id = user.business_id
        if not biz_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            biz_id = biz_row.id if biz_row else "default-biz"
        parked = ParkedCall(
            business_id=biz_id,
            call_id=call.id,
            agent_id=user.id,
            slot=slot_name,
            hold_reason=payload.hold_reason or "PARKED",
            status="ON_HOLD",
        )
        db.add(parked)

    # Set user status to AVAILABLE so they can pick another call if needed
    user.status = "AVAILABLE"
    db.add(user)

    await db.commit()
    await db.refresh(parked)
    return {
        "status": "PARKED",
        "parked_call_id": parked.id,
        "call_id": call.id,
        "slot": slot_name,
        "message": f"Call parked successfully in slot {slot_name}.",
    }


@router.post("/calls/{id}/resume-park")
async def resume_parked_call(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(ParkedCall).where(and_(ParkedCall.call_id == id, ParkedCall.status == "ON_HOLD"))
    parked_list = (await db.execute(stmt)).scalars().all()
    for parked in parked_list:
        parked.status = "RESUMED"
        db.add(parked)

    call = await db.get(Call, id)
    if call:
        call.status = "HUMAN_HANDLING"
        db.add(call)

    user.status = "ON CALL"
    db.add(user)

    await db.commit()
    return {"status": "HUMAN_HANDLING", "message": "Parked call resumed successfully."}


# ---------------------------------------------------------------------------
# 9. 3-Way Conference Connect & Disconnect Client
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/connect-client")
async def connect_client_slot(
    id: str,
    payload: ConnectClientRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business
    client_name = payload.client_name
    client_phone = payload.client_phone

    # If slot provided, lookup from ClientConfig table
    if payload.slot and user.business_id:
        stmt = select(ClientConfig).where(
            and_(ClientConfig.business_id == user.business_id, ClientConfig.slot == payload.slot)
        )
        cfg = (await db.execute(stmt)).scalar_one_or_none()
        if cfg and cfg.client_phone:
            client_name = cfg.client_name or client_name
            client_phone = cfg.client_phone

    if not client_phone:
        raise HTTPException(status_code=400, detail="No client phone number configured. Please set up a client phone in D1/D2/D3 configuration.")

    if not re.match(r"^\+?[1-9]\d{1,14}$", client_phone.strip()):
        raise HTTPException(status_code=400, detail=f"Invalid client phone number: {client_phone}")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call = await db.get(Call, id)
    if not call:
        biz_id = user.business_id
        if not biz_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            biz_id = biz_row.id if biz_row else "default-biz"
        call = Call(
            id=id,
            business_id=biz_id,
            agent_id=await _resolve_ai_agent_id(db, biz_id, user.id),
            from_number=settings.twilio_phone_number or "",
            to_number=settings.twilio_phone_number or "",
            status="CLIENT_ADDING",
            direction="OUTBOUND",
            initiated_at=now,
            connected_at=now,
        )
        db.add(call)
        await db.commit()
        await db.refresh(call)

    try:
        res = await add_client_to_conference_call(
            db=db,
            call=call,
            client_name=client_name or "Client",
            client_phone=client_phone,
            user_id=user.id,
        )
        return res
    except Exception as e:
        call.status = "CLIENT_ADD_FAILED"
        db.add(call)
        await db.commit()
        return {
            "status": "CLIENT_ADD_FAILED",
            "message": f"Failed to dial client: {str(e)}",
            "call_id": call.id,
        }


@router.post("/calls/{id}/disconnect-client")
async def disconnect_client_from_conference(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    call = await db.get(Call, id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
        
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call.client_disconnected_at = now
    
    if call.client_connected_at:
        call.client_duration_seconds = max(0, int((now - call.client_connected_at).total_seconds()))
        
    if call.status == "CONFERENCE_3WAY_ACTIVE":
        call.status = "HUMAN_HANDLING"
        
    # Update participant
    stmt_part = select(CallParticipant).where(
        and_(CallParticipant.call_id == id, CallParticipant.participant_type == "CLIENT", CallParticipant.left_at.is_(None))
    )
    parts = (await db.execute(stmt_part)).scalars().all()
    for p in parts:
        p.left_at = now
        p.duration_seconds = call.client_duration_seconds
        p.status = "DISCONNECTED"
        db.add(p)
        
    db.add(call)
    await db.commit()
    
    utc_now_iso = _to_utc_iso(now)
    from app.api.routes.websockets import global_ws_manager
    await global_ws_manager.broadcast(json.dumps({
        "event": "CLIENT_DISCONNECTED",
        "call_id": call.id,
        "status": call.status,
        "client_duration_seconds": call.client_duration_seconds,
        "client_disconnected_at": utc_now_iso,
    }))
    
    return {
        "status": call.status,
        "client_duration_seconds": call.client_duration_seconds,
        "client_disconnected_at": utc_now_iso,
        "message": "Client disconnected from conference. Customer call remains active.",
    }


@router.get("/campaign-clients")
async def get_voice_campaign_clients(
    campaign_id: Optional[str] = Query(default=None),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    clients_list = []
    seen_ids = set()

    target_campaign = None
    if campaign_id:
        target_campaign = await db.get(Campaign, campaign_id)
    
    if not target_campaign:
        # Resolve campaign for this voice agent
        expert = await db.scalar(select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email)))
        if expert:
            stmt = select(Campaign).where(
                or_(
                    Campaign.voice_agent_id == expert.id,
                    Campaign.agent_id == expert.id
                )
            ).order_by(Campaign.created_at.desc())
            target_campaign = await db.scalar(stmt)
        
        if not target_campaign and user.business_id:
            target_campaign = await db.scalar(select(Campaign).where(Campaign.business_id == user.business_id).order_by(Campaign.created_at.desc()))

    if target_campaign:
        # 1. CampaignLead table
        stmt = (
            select(Lead)
            .join(CampaignLead, CampaignLead.lead_id == Lead.id)
            .where(CampaignLead.campaign_id == target_campaign.id)
        )
        c_leads = (await db.execute(stmt)).scalars().all()
        for l in c_leads:
            if l.id not in seen_ids:
                seen_ids.add(l.id)
                clients_list.append({
                    "id": l.id,
                    "client_id": l.id,
                    "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Customer",
                    "phone": l.phone or "",
                    "email": l.email or "",
                    "company": l.company or "",
                    "location": l.location or "",
                    "status": l.status or "Active",
                    "qualification_status": l.qualification_status or "UNQUALIFIED",
                })

        # 2. LeadGroup fallback
        if target_campaign.lead_group_id:
            stmt_g = select(Lead).where(Lead.group_id == target_campaign.lead_group_id)
            g_leads = (await db.execute(stmt_g)).scalars().all()
            for l in g_leads:
                if l.id not in seen_ids:
                    seen_ids.add(l.id)
                    clients_list.append({
                        "id": l.id,
                        "client_id": l.id,
                        "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Customer",
                        "phone": l.phone or "",
                        "email": l.email or "",
                        "company": l.company or "",
                        "location": l.location or "",
                        "status": l.status or "Active",
                        "qualification_status": l.qualification_status or "UNQUALIFIED",
                    })

        # 3. Campaign Client (fallback only if no customer leads found)
        if not clients_list and target_campaign.client_id:
            c_obj = await db.get(Client, target_campaign.client_id)
            if c_obj and c_obj.id not in seen_ids:
                seen_ids.add(c_obj.id)
                clients_list.append({
                    "id": c_obj.id,
                    "client_id": c_obj.id,
                    "name": c_obj.name or c_obj.company_name or "Primary Client",
                    "phone": c_obj.contact_number or "",
                    "email": c_obj.email or "",
                    "company": c_obj.company_name or "",
                    "location": c_obj.address or "",
                    "status": c_obj.status or "Active",
                    "qualification_status": "QUALIFIED",
                })

    # If no campaign or campaign has no leads yet, fallback to user's assigned leads
    if not clients_list:
        leads_stmt = select(Lead).order_by(Lead.created_at.desc()).limit(100)
        leads_all = (await db.execute(leads_stmt)).scalars().all()
        for l in leads_all:
            if l.id not in seen_ids:
                seen_ids.add(l.id)
                clients_list.append({
                    "id": l.id,
                    "client_id": l.id,
                    "name": f"{l.first_name or ''} {l.last_name or ''}".strip() or "Customer",
                    "phone": l.phone or "",
                    "email": l.email or "",
                    "company": l.company or "",
                    "location": l.location or "",
                    "status": l.status or "Active",
                    "qualification_status": l.qualification_status or "UNQUALIFIED",
                })

    return {
        "campaign_id": target_campaign.id if target_campaign else "",
        "campaign_name": target_campaign.name if target_campaign else "",
        "total_clients": len(clients_list),
        "clients": clients_list,
    }


# ---------------------------------------------------------------------------
# 10. Transfer Call
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/transfer")
async def transfer_call(
    id: str,
    payload: TransferCallRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice import _resolve_or_create_call
    call = await _resolve_or_create_call(db, id, user.id, user.business_id)

    # Register D1 as active call participant if not already registered
    stmt = select(CallParticipant).where(
        CallParticipant.call_id == call.id,
        CallParticipant.participant_type == "AGENT",
        CallParticipant.participant_id == user.id,
        CallParticipant.left_at.is_(None),
    )
    existing_part = await db.scalar(stmt)
    if not existing_part:
        part1 = CallParticipant(
            call_id=call.id,
            participant_type="AGENT",
            participant_id=user.id,
        )
        db.add(part1)

    # Update call status to TRANSFER_REQUESTED
    call.status = "TRANSFER_REQUESTED"
    db.add(call)

    # Check if multiple participants are provided
    if payload.participants:
        dialed_destinations = []
        from app.api.routes.websockets import global_ws_manager
        
        for p in payload.participants:
            dest_phone = p.phone
            dest_name = p.name or p.slot or "Client Contact"
            
            # If client_id is supplied, resolve from Lead or Client table
            if p.client_id:
                lead_obj = await db.get(Lead, p.client_id)
                if lead_obj:
                    dest_name = f"{lead_obj.first_name or ''} {lead_obj.last_name or ''}".strip() or lead_obj.company or dest_name
                    dest_phone = lead_obj.phone or dest_phone
                else:
                    client_obj = await db.get(Client, p.client_id)
                    if client_obj:
                        dest_name = client_obj.name or client_obj.company_name or dest_name
                        dest_phone = client_obj.contact_number or dest_phone

            # Resolve from ClientConfig table if slot is provided and phone still not set
            if not dest_phone and p.slot and user.business_id:
                stmt = select(ClientConfig).where(
                    and_(ClientConfig.business_id == user.business_id, ClientConfig.slot == p.slot)
                )
                cfg = (await db.execute(stmt)).scalar_one_or_none()
                if cfg and cfg.client_phone:
                    dest_phone = cfg.client_phone
                    dest_name = cfg.client_name or dest_name

            if not dest_phone:
                continue

            # Dial and add this client to the conference call
            await add_client_to_conference_call(
                db=db,
                call=call,
                client_name=dest_name,
                client_phone=dest_phone,
                user_id=user.id
            )

            # Register client as CallParticipant in DB
            part = CallParticipant(
                call_id=call.id,
                participant_type="CUSTOMER",
                participant_id=p.client_id or None,
            )
            db.add(part)

            dialed_destinations.append({
                "slot": p.slot,
                "client_id": p.client_id,
                "client_name": dest_name,
                "client_phone": dest_phone
            })

        # Broadcast Webhook or WS event for active 3-way conference
        client_connected_iso = _to_utc_iso(call.client_connected_at or datetime.now(timezone.utc))
        ws_success_payload = {
            "event": "THREE_WAY_ACTIVE",
            "call_id": call.id,
            "status": "CONFERENCE_3WAY_ACTIVE",
            "destinations": dialed_destinations,
            "client_connected_at": client_connected_iso,
        }
        await global_ws_manager.broadcast(json.dumps(ws_success_payload))

        await db.commit()
        return {
            "status": "CONFERENCE_3WAY_ACTIVE",
            "destinations": dialed_destinations,
            "client_connected_at": client_connected_iso,
            "message": "Selected client contact numbers successfully dialed and added to the conference call.",
        }

    # Backward compatible single agent path
    client_id = payload.client_id or call.lead_id
    target_agent_id = payload.target_agent_id or payload.destination_agent_id or user.id

    # Create new CallTransfer record
    transfer = CallTransfer(
        business_id=user.business_id or call.business_id or "default",
        call_id=call.id,
        lead_id=client_id,
        from_agent_id=user.id,
        to_agent_id=target_agent_id,
        transfer_type=payload.transfer_type or "three_way",
        status="PENDING",
    )
    db.add(transfer)
    await db.commit()
    await db.refresh(transfer)
    
    from app.services.hold_music_service import start_customer_hold
    await start_customer_hold(db, call)

    # Fetch client details for real-time alert
    lead = await db.get(Lead, client_id) if client_id else None
    lead_name = f"{lead.first_name} {lead.last_name}".strip() if lead else call.client_name or "Unknown Lead"
    lead_phone = lead.phone if lead else call.client_phone or "Unknown Number"

    # Broadcast WebSocket events
    from app.api.routes.websockets import global_ws_manager
    event_payload = {
        "event": "TRANSFER_REQUESTED",
        "transfer_id": transfer.id,
        "call_id": call.id,
        "client_id": client_id,
        "client_name": lead_name,
        "client_phone": lead_phone,
        "from_agent_id": user.id,
        "from_agent_name": user.full_name or user.email,
        "to_agent_id": target_agent_id,
        "transfer_type": transfer.transfer_type,
        "status": "PENDING"
    }

    # Notify D2
    await global_ws_manager.send_to_user(target_agent_id, event_payload)
    # Notify Supervisor/Business
    await global_ws_manager.broadcast_to_business(user.business_id or "default", {
        "event": "CALL_STATUS_CHANGED",
        "call_id": call.id,
        "status": "TRANSFER_REQUESTED",
        "client_name": lead_name,
        "client_phone": lead_phone,
    })

    return {
        "status": "TRANSFER_REQUESTED",
        "transfer_id": transfer.id,
        "destination_agent_id": target_agent_id,
        "destination_number": payload.destination_number,
        "message": "Call transfer initiated successfully.",
    }


@router.post("/transfers/{transfer_id}/accept")
async def accept_transfer(
    transfer_id: str,
    request: Request,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_agent_transfer import accept_voice_agent_transfer, VoiceAgentTransferAcceptRequest
    try:
        raw = await request.json()
        accept_payload = VoiceAgentTransferAcceptRequest(**raw) if raw else None
    except Exception:
        accept_payload = None
    return await accept_voice_agent_transfer(payload=accept_payload, transfer_id=transfer_id, user=user, db=db)


@router.post("/transfers/{transfer_id}/reject")
async def reject_transfer(
    transfer_id: str,
    request: Request,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_agent_transfer import reject_voice_agent_transfer, VoiceAgentTransferRejectRequest
    try:
        raw = await request.json()
        reject_payload = VoiceAgentTransferRejectRequest(**raw) if raw else None
    except Exception:
        reject_payload = None
    return await reject_voice_agent_transfer(payload=reject_payload, transfer_id=transfer_id, user=user, db=db)



@router.post("/transfers/{transfer_id}/no-answer")
async def no_answer_transfer(
    transfer_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    transfer = await db.get(CallTransfer, transfer_id)
    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer record not found.")

    transfer.status = "NO_ANSWER"

    call = await db.get(Call, transfer.call_id)
    if call:
        call.status = "HUMAN_HANDLING"
        db.add(call)
        
        from app.services.hold_music_service import stop_customer_hold
        await stop_customer_hold(db, call)

    db.add(transfer)
    await db.commit()

    # Broadcast WebSocket event to D1 and business
    from app.api.routes.websockets import global_ws_manager
    event_payload = {
        "event": "TARGET_NO_ANSWER",
        "transfer_id": transfer.id,
        "call_id": transfer.call_id,
        "status": "NO_ANSWER",
    }
    await global_ws_manager.send_to_user(transfer.from_agent_id, event_payload)
    await global_ws_manager.broadcast_to_business(user.business_id or "default", event_payload)

    return {"status": "NO_ANSWER", "message": "Transfer timed out with no answer."}


@router.post("/transfers/{transfer_id}/cancel")
async def cancel_transfer(
    transfer_id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    transfer = await db.get(CallTransfer, transfer_id)
    if not transfer:
        raise HTTPException(status_code=404, detail="Transfer record not found.")

    transfer.status = "CANCELLED"

    call = await db.get(Call, transfer.call_id)
    if call:
        call.status = "HUMAN_HANDLING"
        db.add(call)
        
        from app.services.hold_music_service import stop_customer_hold
        await stop_customer_hold(db, call)

    db.add(transfer)
    await db.commit()

    # Broadcast WebSocket event to D2 and business
    from app.api.routes.websockets import global_ws_manager
    event_payload = {
        "event": "TRANSFER_CANCELLED",
        "transfer_id": transfer.id,
        "call_id": transfer.call_id,
        "status": "CANCELLED",
    }
    await global_ws_manager.send_to_user(transfer.to_agent_id, event_payload)
    await global_ws_manager.broadcast_to_business(user.business_id or "default", event_payload)

    return {"status": "CANCELLED", "message": "Transfer request cancelled."}


@router.get("/transfers")
async def get_my_transfers(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Returns Call Transfer List for logged-in Voice Agent (to_agent_id == user.id)."""
    stmt = (
        select(CallTransfer)
        .where(
            or_(
                CallTransfer.to_agent_id == user.id,
                CallTransfer.from_agent_id == user.id,
            )
        )
        .order_by(CallTransfer.created_at.desc())
    )
    transfers = (await db.execute(stmt)).scalars().all()

    result = []
    for t in transfers:
        lead = await db.get(Lead, t.lead_id) if t.lead_id else None
        call = await db.get(Call, t.call_id) if t.call_id else None
        from_user = await db.get(User, t.from_agent_id) if t.from_agent_id else None

        campaign_id = t.campaign_id or (call.campaign_id if call else None)
        campaign = await db.get(Campaign, campaign_id) if campaign_id else None

        client_id = t.client_id or (campaign.client_id if campaign else (call.client_id if call else None))
        client = await db.get(Client, client_id) if client_id else None

        customer_name = (
            f"{lead.first_name} {lead.last_name}".strip()
            if lead and (lead.first_name or lead.last_name)
            else (call.client_name if call else "Customer")
        )
        mobile = lead.phone if lead else (call.client_phone if call else "Unknown Number")
        from_name = from_user.full_name or from_user.email if from_user else "AI Agent"

        campaign_name = campaign.name if campaign else ""
        client_name = client.name if client else (campaign.client_name if campaign else (call.client_name if call else "Client"))
        client_company = client.company_name if client else client_name

        result.append({
            "transfer_id": t.id,
            "call_id": t.call_id,
            "lead_id": t.lead_id,
            "campaign_id": campaign.id if campaign else None,
            "campaign_name": campaign_name,
            "client_id": client.id if client else None,
            "client_name": client_name,
            "client_company": client_company,
            "customer_name": customer_name,
            "mobile": mobile,
            "from_agent_name": from_name,
            "reason": t.transfer_type or "Requested Human Agent",
            "status": t.status,
            "time": t.created_at.isoformat() if t.created_at else "",
        })

    return result


@router.post("/calls/{id}/leave-transfer")
async def leave_transfer(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    # Fetch active transfer for this call
    stmt = select(CallTransfer).where(
        CallTransfer.call_id == id,
        CallTransfer.status.in_(["TARGET_CONNECTED", "ACCEPTED", "PENDING"])
    ).order_by(CallTransfer.created_at.desc())
    transfer = await db.scalar(stmt)

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Update participant left_at time
    stmt_part = select(CallParticipant).where(
        CallParticipant.call_id == id,
        CallParticipant.participant_id == user.id,
        CallParticipant.left_at.is_(None)
    )
    part = await db.scalar(stmt_part)
    if part:
        part.left_at = now
        part.status = "LEFT"
        db.add(part)

    # Check remaining active participants in the call
    stmt_remaining = select(CallParticipant).where(
        CallParticipant.call_id == id,
        CallParticipant.participant_id != user.id,
        CallParticipant.left_at.is_(None)
    )
    remaining_parts = (await db.execute(stmt_remaining)).scalars().all()
    remaining_agents = [p for p in remaining_parts if p.participant_type == "AGENT"]

    call = await db.get(Call, id)
    if call:
        if remaining_agents:
            # Another agent remains on the call with customer; transition call to HUMAN_HANDLING
            call.status = "HUMAN_HANDLING"
            call.agent_id = remaining_agents[0].participant_id
            if transfer:
                transfer.status = "COMPLETED"
                db.add(transfer)
        else:
            # No agents remain on the call; call wraps up
            call.status = "COMPLETED"
            call.ended_at = now
            if transfer:
                transfer.status = "COMPLETED"
                db.add(transfer)
        db.add(call)

    await db.commit()

    # Broadcast WebSocket event
    from app.api.routes.websockets import global_ws_manager
    if remaining_agents:
        event_payload = {
            "event": "PARTICIPANT_LEFT",
            "type": "PARTICIPANT_LEFT",
            "call_id": id,
            "status": "HUMAN_HANDLING",
            "left_participant_id": user.id,
            "remaining_agent_id": remaining_agents[0].participant_id,
        }
        await global_ws_manager.broadcast_to_business(user.business_id or "default", event_payload)
        await global_ws_manager.send_to_user(remaining_agents[0].participant_id, event_payload)
        # Notify the leaving agent only that their session ended
        await global_ws_manager.send_to_user(user.id, {
            "event": "CALL_ENDED",
            "call_id": id,
            "status": "COMPLETED",
        })
    else:
        event_payload = {
            "event": "TRANSFER_COMPLETED",
            "call_id": id,
            "status": "COMPLETED",
            "left_agent_id": user.id,
        }
        await global_ws_manager.broadcast_to_business(user.business_id or "default", event_payload)

    return {
        "status": "SUCCESS",
        "message": "You left the call. Other participants remain connected." if remaining_agents else "You left the call. Call completed."
    }


@router.get("/calls/{id}/transfer-history")
async def get_transfer_history(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(CallTransfer).where(CallTransfer.call_id == id).order_by(CallTransfer.created_at.asc())
    transfers = await db.scalars(stmt)

    history = []

    # Build a nice structured milestone timeline
    for t in transfers.all():
        from_user = await db.get(User, t.from_agent_id)
        to_user = await db.get(User, t.to_agent_id)
        history.append({
            "timestamp": t.created_at.replace(tzinfo=timezone.utc).isoformat(),
            "event": f"Agent {from_user.full_name if from_user else 'Unknown'} initiated {t.transfer_type} transfer to {to_user.full_name if to_user else 'Unknown'}",
            "status": t.status,
        })
        if t.status == "TARGET_CONNECTED":
            history.append({
                "timestamp": t.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                "event": f"Agent {to_user.full_name if to_user else 'Unknown'} accepted the transfer. 3-way conference active.",
                "status": t.status,
            })
        elif t.status == "COMPLETED":
            history.append({
                "timestamp": t.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                "event": f"Agent {from_user.full_name if from_user else 'Unknown'} left the call. Transfer complete.",
                "status": t.status,
            })
        elif t.status == "REJECTED":
            history.append({
                "timestamp": t.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                "event": f"Agent {to_user.full_name if to_user else 'Unknown'} rejected the transfer.",
                "status": t.status,
            })
        elif t.status == "CANCELLED":
            history.append({
                "timestamp": t.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                "event": f"Agent {from_user.full_name if from_user else 'Unknown'} cancelled the transfer request.",
                "status": t.status,
            })
        elif t.status == "NO_ANSWER":
            history.append({
                "timestamp": t.updated_at.replace(tzinfo=timezone.utc).isoformat(),
                "event": f"Transfer timed out. Agent {to_user.full_name if to_user else 'Unknown'} did not answer.",
                "status": t.status,
            })

    return history


# ---------------------------------------------------------------------------
# 11. Recording Controls
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/recording/start")
async def start_call_recording(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    call = await db.get(Call, id)
    rec_id = f"REC-{id[:8].upper()}"
    rec_url = f"/recordings/{id}_rec.wav"

    if call:
        call.recording_url = rec_url
        db.add(call)
        await db.commit()

    return {
        "status": "RECORDING_ACTIVE",
        "record_id": rec_id,
        "recording_url": rec_url,
        "start_time": datetime.now(timezone.utc).strftime("%I:%M:%S %p"),
    }


@router.post("/calls/{id}/recording/stop")
async def stop_call_recording(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    return {
        "status": "RECORDING_STOPPED",
        "record_id": f"REC-{id[:8].upper()}",
        "message": "Recording stopped and saved.",
    }


# ---------------------------------------------------------------------------
# 12. End Call
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/end")
async def end_call(
    id: str,
    payload: EndCallRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business
    call = await db.get(Call, id)
    target_lead_id = payload.lead_id or (call.lead_id if call else None)
    if not target_lead_id:
        lead_check = await db.get(Lead, id)
        if lead_check:
            target_lead_id = lead_check.id

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if not call:
        dur = payload.duration_seconds or 0
        biz_id = user.business_id
        if not biz_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            biz_id = biz_row.id if biz_row else "default-biz"
        from app.services.recording_service import ensure_call_recording
        rec_url = ensure_call_recording(id, duration_sec=dur or 15)
        call = Call(
            id=id,
            business_id=biz_id,
            lead_id=target_lead_id,
            agent_id=user.id,  # Updated: Assigning call.agent_id to user.id
            from_number=settings.twilio_phone_number or "",
            to_number=settings.twilio_phone_number or "",
            status="COMPLETED",
            duration_seconds=dur,
            recording_url=rec_url,
            call_result=payload.outcome or "Completed",
            initiated_at=now - timedelta(seconds=dur + 5),
            connected_at=now - timedelta(seconds=dur),
            ended_at=now,
        )
        db.add(call)
    else:
        if not call.agent_id:
            call.agent_id = user.id  # Updated: Assigning call.agent_id to user.id
        call.status = "COMPLETED"
        call.ended_at = now

        # Terminate live provider leg on Twilio if active
        if call.call_sid and settings.twilio_account_sid and settings.twilio_auth_token:
            try:
                import asyncio
                from twilio.rest import Client
                client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
                def _terminate_leg():
                    try:
                        return client.calls(call.call_sid).update(status="completed")
                    except Exception as t_err:
                        logger.debug(f"Twilio call termination notice for {call.call_sid}: {t_err}")
                        return None
                await asyncio.to_thread(_terminate_leg)
            except Exception as leg_err:
                logger.debug(f"Provider call termination notice: {leg_err}")

        # Terminate all live Asterisk / SIP legs (customer and client legs)
        try:
            from app.services.voice_service import hangup_sip_call
            hangup_sip_call(call.id)
        except Exception as sip_err:
            logger.debug(f"SIP hangup notice for {call.id}: {sip_err}")
        
        # Calculate real duration from payload, connected_at, or initiated_at
        dur = 0
        if payload.duration_seconds and payload.duration_seconds > 0:
            dur = payload.duration_seconds
        elif call.connected_at:
            dur = max(0, int((now - call.connected_at).total_seconds()))
        elif call.initiated_at:
            dur = max(0, int((now - call.initiated_at).total_seconds()))

        if dur == 0 and (call.initiated_at or call.connected_at):
            base_t = call.connected_at or call.initiated_at
            dur = max(1, int((now - base_t).total_seconds()))

        call.duration_seconds = dur
        if not call.connected_at:
            call.connected_at = now - timedelta(seconds=dur)
            
        if payload.outcome:
            call.call_result = payload.outcome
            
        # If client was connected without explicit disconnect, finalize client duration
        if call.client_connected_at and not call.client_disconnected_at:
            call.client_disconnected_at = now
            call.client_duration_seconds = max(0, int((now - call.client_connected_at).total_seconds()))

        if not call.recording_url:
            clean_status = (call.status or "").upper().strip()
            if clean_status not in ("FAILED", "BUSY", "NO_ANSWER", "NO-ANSWER", "MISSED", "CANCELED", "CANCELLED", "REJECTED") and (dur > 0 or call.transcript):
                from app.services.recording_service import ensure_call_recording
                rec_u = ensure_call_recording(call.id, duration_sec=dur or 15)
                if rec_u:
                    call.recording_url = rec_u

        db.add(call)

    # Close any active hold/park for this call
    stmt_park_close = select(ParkedCall).where(and_(ParkedCall.call_id == id, ParkedCall.status == "ON_HOLD"))
    active_parked = (await db.execute(stmt_park_close)).scalars().all()
    for pk in active_parked:
        pk.status = "COMPLETED"
        db.add(pk)

    # End active participants
    stmt_parts = select(CallParticipant).where(and_(CallParticipant.call_id == id, CallParticipant.left_at.is_(None)))
    active_parts = (await db.execute(stmt_parts)).scalars().all()
    for ap in active_parts:
        ap.left_at = now
        if ap.joined_at:
            ap.duration_seconds = max(0, int((now - ap.joined_at).total_seconds()))
        ap.status = "ENDED"
        db.add(ap)

    # Determine if call was actually answered/attended by customer
    is_answered_call = bool(
        call and (
            call.connected_at is not None or 
            (call.duration_seconds or 0) > 0 or 
            (payload.duration_seconds or 0) > 0
        ) and (
            (payload.outcome or "").upper() not in ["NO_ANSWER", "BUSY", "FAILED", "REJECTED", "UNREACHABLE", "CANCELLED", "TIMEOUT"]
        )
    )

    raw_outcome = (payload.outcome or "").strip()
    is_callback = bool(
        raw_outcome and (
            "CALLBACK" in raw_outcome.upper() or
            "CALL BACK" in raw_outcome.upper()
        )
    )

    if is_callback:
        call_status_str = "CALL BACK"
        disposal_status_str = raw_outcome or "CALL BACK"
        lead_final_status = "CALL BACK"
    elif is_answered_call:
        call_status_str = "COMPLETED"
        disposal_status_str = raw_outcome or "Connected"
        lead_final_status = "COMPLETED"
    elif raw_outcome:
        call_status_str = "No Answer" if (raw_outcome.upper() in ["NO_ANSWER", "NO ANSWER", "BUSY", "FAILED", "REJECTED", "UNREACHABLE", "CANCELLED", "TIMEOUT"]) else raw_outcome
        disposal_status_str = raw_outcome
        lead_final_status = raw_outcome.upper()
    else:
        call_status_str = "No Answer"
        disposal_status_str = "No Answer"
        lead_final_status = "NO ANSWER"

    # Ensure current user has an Expert record ONLY if they are an actual voice agent
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    biz_id = user.business_id
    if not biz_id:
        biz_row = (await db.execute(select(Business))).scalars().first()
        biz_id = biz_row.id if biz_row else "default-biz"
    is_voice_agent_user = bool(user.role and user.role.upper() in ["VOICE_AGENT", "AGENT", "INBOUND_AGENT", "OUTBOUND_AGENT"])
    if not expert and is_voice_agent_user:
        agent_name_str = getattr(user, "full_name", None) or (user.email.split("@")[0] if getattr(user, "email", None) else "Agent")
        expert = Expert(
            business_id=biz_id,
            user_id=user.id,
            name=agent_name_str,
            email=user.email or "",
            status="AVAILABLE",
        )
        db.add(expert)
        await db.flush()

    # Update associated Lead and CampaignLead statuses
    if target_lead_id:
        lead = await db.get(Lead, target_lead_id)
        if lead:
            lead.call_status = call_status_str
            lead.call_duration = call.duration_seconds if call else (payload.duration_seconds or 0)
            lead.call_result = disposal_status_str
            lead.status = lead_final_status
            lead.assigned_expert_id = expert.id if expert else None
            if payload.qualification_status:
                lead.qualification_status = payload.qualification_status
            db.add(lead)

            # Non-breaking Side-effect: Follow-up email on call disposition if lead has email
            if lead.email and "@" in lead.email:
                try:
                    from app.services.email_service import send_email
                    agent_display_name = (expert.name if expert else (user.full_name or "CallZenza Support")) or "CallZenza Support"
                    subj = f"Call Summary & Follow-up — {agent_display_name}"
                    body_html = (
                        f"<div style='font-family: Arial, sans-serif; max-width: 600px; padding: 20px; border: 1px solid #e2e8f0; border-radius: 8px;'>"
                        f"<h2 style='color: #4f46e5; margin-top: 0;'>Hello {lead.first_name or 'there'},</h2>"
                        f"<p>Thank you for speaking with our team today. Here is a brief record of your call outcome:</p>"
                        f"<ul style='line-height: 1.6; color: #334155;'>"
                        f"<li><strong>Status / Disposition:</strong> {disposal_status_str}</li>"
                        f"<li><strong>Representative:</strong> {agent_display_name}</li>"
                        f"<li><strong>Duration:</strong> {lead.call_duration or 0} seconds</li>"
                        f"</ul>"
                        f"<p>If you have any questions or require further assistance, please feel free to reply to this email.</p>"
                        f"<hr style='border: none; border-top: 1px solid #e2e8f0; margin: 20px 0;'/>"
                        f"<p style='font-size: 11px; color: #94a3b8;'>CallZenza Operations Platform · Automated Notification</p>"
                        f"</div>"
                    )
                    await send_email(
                        to_email=lead.email.strip(),
                        subject=subj,
                        html_content=body_html,
                        business_id=biz_id,
                        db=db,
                    )
                    logger.info(f"[DISPOSITION EMAIL] Follow-up email queued for lead {lead.email}")
                except Exception as e_mail:
                    logger.warning(f"[DISPOSITION EMAIL ERROR] Non-breaking exception: {e_mail}")

        # Update CampaignLead status
        c_lead_stmt = select(CampaignLead).where(
            or_(CampaignLead.call_id == id, CampaignLead.lead_id == target_lead_id)
        )
        c_leads = (await db.execute(c_lead_stmt)).scalars().all()
        for cl in c_leads:
            cl.status = "CALL BACK" if is_callback else disposal_status_str
            cl.call_id = id
            cl.assigned_expert_id = expert.id
            db.add(cl)

        # Update LeadAssignment
        from app.db.models import LeadAssignment
        assign_stmt = select(LeadAssignment).where(LeadAssignment.lead_id == target_lead_id)
        assigns = (await db.execute(assign_stmt)).scalars().all()
        agent_disp_name = getattr(user, "full_name", None) or (user.email.split("@")[0] if getattr(user, "email", None) else "Agent")
        if assigns:
            for assign in assigns:
                assign.expert_id = expert.id
                assign.ai_agent_name = agent_disp_name
                if is_callback:
                    assign.status = "ACTIVE"
                else:
                    assign.status = "COMPLETED"
                    assign.completed_at = now
                db.add(assign)
        else:
            new_assign = LeadAssignment(
                business_id=biz_id,
                lead_id=target_lead_id,
                expert_id=expert.id,
                status="ACTIVE" if is_callback else "COMPLETED",
                trigger_source="VOICE_CALL",
                ai_agent_name=agent_disp_name,
            )
            if not is_callback:
                new_assign.completed_at = now
            db.add(new_assign)

    # End any active parked call
    stmt = select(ParkedCall).where(and_(ParkedCall.call_id == id, ParkedCall.status == "ON_HOLD"))
    parked_list = (await db.execute(stmt)).scalars().all()
    for p in parked_list:
        p.status = "ENDED"
        db.add(p)

    user.status = "AVAILABLE"
    db.add(user)

    await db.commit()
    if call:
        await db.refresh(call)

    utc_now_iso = _to_utc_iso(now)
    from app.api.routes.websockets import global_ws_manager
    await global_ws_manager.broadcast(json.dumps({
        "event": "CALL_COMPLETED",
        "call_id": call.id if call else id,
        "status": "COMPLETED",
        "duration_seconds": call.duration_seconds if call else 0,
        "ended_at": utc_now_iso,
    }))

    return {
        "status": "COMPLETED",
        "call_id": call.id if call else id,
        "duration_seconds": call.duration_seconds if call else 0,
        "ended_at": utc_now_iso,
        "message": "Call ended successfully.",
    }


# ---------------------------------------------------------------------------
# 13. Dial Next & Customer Data
# ---------------------------------------------------------------------------


@router.get("/leads/lists")
async def get_lead_lists(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    biz_id = user.business_id or (expert.business_id if expert else None)
    if not biz_id and (user.role or "").upper() != "SUPER_ADMIN":
        return []

    # Find campaigns matching this tenant and agent
    if (user.role or "").upper() in ("ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER"):
        stmt_camp = select(Campaign).where(Campaign.business_id == biz_id).order_by(Campaign.created_at.desc())
    else:
        exp_id = expert.id if expert else ""
        stmt_camp = select(Campaign).where(
            Campaign.business_id == biz_id,
            or_(
                Campaign.voice_agent_id == exp_id,
                Campaign.agent_id == exp_id,
                Campaign.voice_agent_id == user.id,
                Campaign.agent_id == user.id,
                Campaign.business_id == biz_id
            )
        ).order_by(Campaign.created_at.desc())
    campaigns = (await db.execute(stmt_camp)).scalars().all()

    results = []
    excluded_statuses = ["COMPLETED", "DISQUALIFIED", "DNC", "PROCESSED", "CALLED"]
    existing_group_ids = set()

    for camp in campaigns:
        group_id = camp.lead_group_id
        if group_id:
            existing_group_ids.add(group_id)
            lg_obj = await db.get(LeadGroup, group_id)
            disp_name = (lg_obj.filename if lg_obj else None) or camp.name
            
            total_stmt = select(func.count(Lead.id)).where(Lead.group_id == group_id)
            available_stmt = select(func.count(Lead.id)).where(Lead.group_id == group_id, Lead.status.notin_(excluded_statuses))
            
            total_count = (await db.execute(total_stmt)).scalar() or 0
            available_count = (await db.execute(available_stmt)).scalar() or 0
            called_count = total_count - available_count
            
            results.append({
                "id": group_id,
                "campaign_id": camp.id,
                "name": disp_name,
                "campaign_name": camp.name,
                "total_leads": total_count,
                "available_leads": available_count,
                "called_leads": called_count,
                "pending_leads": available_count,
                "is_campaign_list": False
            })
        else:
            # Fallback to campaign_leads count for direct campaign lists
            total_stmt = select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == camp.id)
            available_stmt = select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == camp.id,
                CampaignLead.status.notin_(excluded_statuses)
            )

            total_count = (await db.execute(total_stmt)).scalar() or 0
            available_count = (await db.execute(available_stmt)).scalar() or 0
            called_count = total_count - available_count
            
            if total_count > 0:
                results.append({
                    "id": camp.id,
                    "campaign_id": camp.id,
                    "name": camp.name,
                    "campaign_name": camp.name,
                    "total_leads": total_count,
                    "available_leads": available_count,
                    "called_leads": called_count,
                    "pending_leads": available_count,
                    "is_campaign_list": True
                })

    # Return ALL LeadGroups under this business whether attached to campaigns or unassigned
    if biz_id:
        stmt_groups = select(LeadGroup).where(
            LeadGroup.business_id == biz_id
        ).order_by(LeadGroup.created_at.desc())
        unassigned_groups = (await db.execute(stmt_groups)).scalars().all()
        for lg in unassigned_groups:
            if lg.id in existing_group_ids:
                continue
            total_stmt = select(func.count(Lead.id)).where(Lead.group_id == lg.id)
            available_stmt = select(func.count(Lead.id)).where(Lead.group_id == lg.id, Lead.status.notin_(excluded_statuses))
            t_cnt = (await db.execute(total_stmt)).scalar() or 0
            a_cnt = (await db.execute(available_stmt)).scalar() or 0
            results.append({
                "id": lg.id,
                "campaign_id": None,
                "name": lg.filename,
                "campaign_name": lg.filename,
                "total_leads": t_cnt,
                "available_leads": a_cnt,
                "called_leads": t_cnt - a_cnt,
                "pending_leads": a_cnt,
                "is_campaign_list": False
            })

    return results


@router.get("/leads/list-customers")
async def get_lead_list_customers(
    campaign_id: Optional[str] = Query(None),
    lead_group_id: Optional[str] = Query(None),
    only_active: Optional[bool] = Query(False),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns all customer leads inside a specific lead list.
    Requires campaign_id or lead_group_id. Returns empty list if neither is provided.
    """
    if not campaign_id and not lead_group_id:
        return []

    target_camp_id = campaign_id
    target_group_id = lead_group_id

    # If lead_group_id matches a Campaign ID, treat it cleanly
    if target_group_id and not target_camp_id:
        camp_check = await db.execute(select(Campaign).where(Campaign.id == target_group_id))
        c_match = camp_check.scalars().first()
        if c_match:
            target_camp_id = c_match.id
            target_group_id = c_match.lead_group_id

    customers = []
    completed_statuses = {"COMPLETED", "QUALIFIED", "NOT_QUALIFIED", "DISPOSITIONED", "FINISHED"}
    expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
    expert = (await db.execute(expert_stmt)).scalars().first()
    biz_id = user.business_id or (expert.business_id if expert else None)
    if not biz_id and (user.role or "").upper() != "SUPER_ADMIN":
        return []

    if target_group_id:
        lg_obj = await db.get(LeadGroup, target_group_id)
        if lg_obj and biz_id and lg_obj.business_id != biz_id and (user.role or "").upper() != "SUPER_ADMIN":
            return []
        stmt = (
            select(Lead)
            .where(Lead.group_id == target_group_id)
        )
        if biz_id:
            stmt = stmt.where(Lead.business_id == biz_id)
        stmt = stmt.order_by(Lead.created_at.asc())
        rows = (await db.execute(stmt)).scalars().all()
        for lead in rows:
            if only_active and ((lead.status or "").upper() in completed_statuses or (lead.call_status or "").upper() in completed_statuses):
                continue
            customers.append({
                "id": lead.id,
                "first_name": lead.first_name or "",
                "last_name": lead.last_name or "",
                "phone": lead.phone or "",
                "email": lead.email or "",
                "company": lead.company or "",
                "requirement": lead.requirement or "",
                "location": lead.location or "",
                "status": lead.status or "NEW",
                "call_status": lead.call_status or "PENDING",
                "qualification_status": (lead.qualification_status or "UNQUALIFIED").upper(),
                "call_result": lead.call_result or "",
                "source": lead.source or "",
            })
    elif target_camp_id:
        camp_obj = await db.get(Campaign, target_camp_id)
        if not camp_obj or (biz_id and camp_obj.business_id != biz_id and (user.role or "").upper() != "SUPER_ADMIN"):
            return []
        if camp_obj.lead_group_id:
            stmt = select(Lead).where(Lead.group_id == camp_obj.lead_group_id)
            if biz_id:
                stmt = stmt.where(Lead.business_id == biz_id)
            stmt = stmt.order_by(Lead.created_at.asc())
            rows = (await db.execute(stmt)).scalars().all()
            for lead in rows:
                if only_active and ((lead.status or "").upper() in completed_statuses or (lead.call_status or "").upper() in completed_statuses):
                    continue
                customers.append({
                    "id": lead.id,
                    "first_name": lead.first_name or "",
                    "last_name": lead.last_name or "",
                    "phone": lead.phone or "",
                    "email": lead.email or "",
                    "company": lead.company or "",
                    "requirement": lead.requirement or "",
                    "location": lead.location or "",
                    "status": lead.status or "NEW",
                    "call_status": lead.call_status or "PENDING",
                    "qualification_status": (lead.qualification_status or "UNQUALIFIED").upper(),
                    "call_result": lead.call_result or "",
                    "source": lead.source or "",
                })
        else:
            stmt = (
                select(Lead, CampaignLead.status.label("campaign_status"))
                .join(CampaignLead, CampaignLead.lead_id == Lead.id)
                .where(CampaignLead.campaign_id == target_camp_id)
            )
            if biz_id:
                stmt = stmt.where(Lead.business_id == biz_id)
            stmt = stmt.order_by(Lead.created_at.asc())
            rows = (await db.execute(stmt)).all()
            for row in rows:
                lead = row[0]
                camp_status = row[1]
                if only_active and ((lead.status or "").upper() in completed_statuses or (lead.call_status or "").upper() in completed_statuses or (camp_status or "").upper() in completed_statuses):
                    continue
                customers.append({
                    "id": lead.id,
                    "first_name": lead.first_name or "",
                    "last_name": lead.last_name or "",
                    "phone": lead.phone or "",
                    "email": lead.email or "",
                    "company": lead.company or "",
                    "requirement": lead.requirement or "",
                    "location": lead.location or "",
                    "status": lead.status or "NEW",
                    "call_status": camp_status or lead.call_status or "PENDING",
                    "qualification_status": (lead.qualification_status or "UNQUALIFIED").upper(),
                    "call_result": lead.call_result or "",
                    "source": lead.source or "",
                })

    return customers


@router.get("/leads/next")
async def get_next_dial_lead(
    current_lead_id: Optional[str] = Query(None),
    exclude_lead_ids: Optional[str] = Query(None),
    campaign_id: Optional[str] = Query(None),
    lead_group_id: Optional[str] = Query(None),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        # 1. Fetch matching Expert record for current user
        expert_stmt = select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
        expert = (await db.execute(expert_stmt)).scalars().first()

        logger.info(f"[Dial Next] Request received from User {user.id} ({user.email}) | Expert ID: {expert.id if expert else None} | Current Lead: {current_lead_id} | Campaign ID: {campaign_id} | Group ID: {lead_group_id}")

        # Build excluded IDs set from current lead and explicit excluded IDs list
        all_excluded_ids: set[str] = set()
        if current_lead_id and current_lead_id.strip():
            curr_id = current_lead_id.strip()
            try:
                curr_lead_obj = await db.get(Lead, curr_id)
                is_curr_cb = bool(
                    curr_lead_obj and (
                        curr_lead_obj.status == "CALL BACK" or
                        curr_lead_obj.call_status == "CALL BACK" or
                        "CALLBACK" in (curr_lead_obj.call_result or "").upper() or
                        "CALL BACK" in (curr_lead_obj.call_result or "").upper()
                    )
                )
                if not is_curr_cb:
                    all_excluded_ids.add(curr_id)
                    if curr_lead_obj:
                        curr_lead_obj.status = "COMPLETED"
                        curr_lead_obj.call_status = "COMPLETED"
                        db.add(curr_lead_obj)
                    
                    curr_cl_stmt = select(CampaignLead).where(CampaignLead.lead_id == curr_id)
                    curr_cls = (await db.execute(curr_cl_stmt)).scalars().all()
                    for cl in curr_cls:
                        cl.status = "COMPLETED"
                        db.add(cl)
                    await db.commit()
                else:
                    # Current lead is marked for CALL BACK: keep it dialable and preserve its status!
                    pass
            except Exception as e:
                logger.warning(f"Note updating current lead status: {e}")
                await db.rollback()

        if exclude_lead_ids and exclude_lead_ids.strip():
            for eid in exclude_lead_ids.split(","):
                clean_id = eid.strip()
                if clean_id:
                    all_excluded_ids.add(clean_id)

        excluded_statuses = ["COMPLETED", "DISQUALIFIED", "DNC", "PROCESSED", "CALLED", "CALLING", "CALL_IN_PROGRESS", "CONNECTED", "RECYCLED", "QUALIFIED"]

        current_biz_id = user.business_id or (expert.business_id if expert else None)
        campaign = None
        if campaign_id:
            campaign = await db.get(Campaign, campaign_id)
            if campaign and current_biz_id and campaign.business_id != current_biz_id and (user.role or "").upper() != "SUPER_ADMIN":
                campaign = None
        elif expert and expert.id and current_biz_id:
            stmt_camp = select(Campaign).where(
                Campaign.business_id == current_biz_id,
                or_(Campaign.voice_agent_id == expert.id, Campaign.agent_id == expert.id)
            ).order_by(Campaign.created_at.desc())
            campaign = (await db.execute(stmt_camp)).scalars().first()

        if not campaign and current_biz_id:
            # 2nd Priority: Find campaign matching user or expert business_id
            stmt_camp = select(Campaign).where(Campaign.business_id == current_biz_id).order_by(Campaign.created_at.desc())
            campaign = (await db.execute(stmt_camp)).scalars().first()

        if not campaign and not lead_group_id:
            return {
                "lead": None,
                "campaign": None,
                "total_count": 0,
                "remaining_count": 0,
                "message": "No lead list selected."
            }

        logger.info(f"[Dial Next] Resolved Campaign: {campaign.name if campaign else 'None'} (ID: {campaign.id if campaign else 'None'})")

        # 3. Fetch Next Eligible Lead from Assigned Campaign or System
        next_c_lead = None
        next_lead = None
        total_count = 0
        current_biz_id = user.business_id or (expert.business_id if expert else None)

        if campaign:
            # Log total leads in campaign
            total_c_leads_stmt = select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id == campaign.id)
            total_count = (await db.execute(total_c_leads_stmt)).scalar() or 0

            # Query CampaignLead JOIN Lead for eligible leads in deterministic FIFO order
            query_cl = (
                select(CampaignLead, Lead)
                .join(Lead, CampaignLead.lead_id == Lead.id)
                .where(
                    CampaignLead.campaign_id == campaign.id,
                    CampaignLead.status.notin_(excluded_statuses),
                    Lead.status.notin_(excluded_statuses),
                    or_(Lead.call_status.is_(None), Lead.call_status.notin_(excluded_statuses)),
                )
            )
            if expert and expert.id:
                # Voice Agent: route ONLY leads assigned to this Voice Agent (or unassigned in this campaign)
                query_cl = query_cl.where(
                    or_(
                        CampaignLead.assigned_expert_id == expert.id,
                        Lead.assigned_expert_id == expert.id,
                        and_(
                            CampaignLead.assigned_expert_id.is_(None),
                            Lead.assigned_expert_id.is_(None),
                        ),
                    )
                )
            else:
                # Non-voice-agent / AI agent: CANNOT access leads assigned to any Voice Agent!
                query_cl = query_cl.where(
                    CampaignLead.assigned_expert_id.is_(None),
                    Lead.assigned_expert_id.is_(None),
                )
            if lead_group_id:
                query_cl = query_cl.where(Lead.group_id == lead_group_id)
            if all_excluded_ids:
                query_cl = query_cl.where(
                    and_(
                        Lead.id.notin_(list(all_excluded_ids)),
                        CampaignLead.lead_id.notin_(list(all_excluded_ids))
                    )
                )
                
            query_cl = query_cl.order_by(CampaignLead.created_at.asc(), Lead.created_at.asc(), Lead.first_name.asc(), Lead.phone.asc(), CampaignLead.id.asc())
            res_cl = (await db.execute(query_cl)).first()
            if res_cl:
                next_c_lead, next_lead = res_cl[0], res_cl[1]
            elif lead_group_id or campaign.lead_group_id:
                active_group_id = lead_group_id or campaign.lead_group_id
                if total_count == 0:
                    total_group_stmt = select(func.count(Lead.id)).where(Lead.group_id == active_group_id)
                    total_count = (await db.execute(total_group_stmt)).scalar() or 0

                query_group = (
                    select(Lead)
                    .where(
                        Lead.group_id == active_group_id,
                        Lead.status.notin_(excluded_statuses),
                    )
                )
                if expert and expert.id:
                    query_group = query_group.where(
                        or_(
                            Lead.assigned_expert_id == expert.id,
                            Lead.assigned_expert_id.is_(None),
                        )
                    )
                else:
                    query_group = query_group.where(
                        Lead.assigned_expert_id.is_(None)
                    )
                if all_excluded_ids:
                    query_group = query_group.where(Lead.id.notin_(list(all_excluded_ids)))
                query_group = query_group.order_by(Lead.created_at.asc(), Lead.id.asc())
                next_lead = (await db.execute(query_group)).scalars().first()

        current_biz_id = user.business_id or (expert.business_id if expert else None)
        if not next_lead and lead_group_id:
            total_group_stmt = select(func.count(Lead.id)).where(Lead.group_id == lead_group_id)
            if current_biz_id:
                total_group_stmt = total_group_stmt.where(Lead.business_id == current_biz_id)
            total_count = (await db.execute(total_group_stmt)).scalar() or 0

            query_group = (
                select(Lead)
                .where(
                    Lead.group_id == lead_group_id,
                    Lead.status.notin_(excluded_statuses),
                    or_(Lead.call_status.is_(None), Lead.call_status.notin_(excluded_statuses)),
                )
            )
            if current_biz_id:
                query_group = query_group.where(Lead.business_id == current_biz_id)
            if all_excluded_ids:
                query_group = query_group.where(Lead.id.notin_(list(all_excluded_ids)))
            query_group = query_group.order_by(Lead.created_at.asc(), Lead.id.asc())
            next_lead = (await db.execute(query_group)).scalars().first()
        elif not next_lead and not campaign_id and current_biz_id:
            if total_count == 0:
                total_lead_stmt = select(func.count(Lead.id)).where(Lead.business_id == current_biz_id)
                total_count = (await db.execute(total_lead_stmt)).scalar() or 0

            query_group = (
                select(Lead)
                .where(
                    Lead.business_id == current_biz_id,
                    Lead.status.notin_(excluded_statuses),
                    or_(Lead.call_status.is_(None), Lead.call_status.notin_(excluded_statuses)),
                )
            )
            if all_excluded_ids:
                query_group = query_group.where(Lead.id.notin_(list(all_excluded_ids)))
            query_group = query_group.order_by(Lead.created_at.asc(), Lead.id.asc())
            next_lead = (await db.execute(query_group)).scalars().first()

        if not next_lead:
            logger.info(f"[Dial Next] All eligible leads have been dialed/completed for Campaign: {campaign.id if campaign else 'None'} | Group: {lead_group_id}")
            return {
                "lead": None,
                "campaign": None,
                "total_count": total_count,
                "remaining_count": 0,
                "message": "No more customers available in the selected lead list."
            }

        logger.info(f"[Dial Next] Successfully selected Lead ID: {next_lead.id} ({next_lead.first_name} {next_lead.last_name}, Phone: {next_lead.phone})")

        # 5. Lock lead status atomically for active call session
        try:
            if next_c_lead:
                next_c_lead.status = "CALLING"
                db.add(next_c_lead)
            next_lead.call_status = "CALLING"
            db.add(next_lead)
            await db.commit()
        except Exception as e:
            logger.warning(f"Note updating lead calling status: {e}")
            await db.rollback()

        lead_name = f"{next_lead.first_name or ''} {next_lead.last_name or ''}".strip() or "Unknown Lead"

        # 6. Extract Campaign Script and details
        script_text = f"Hi {lead_name}, my name is {user.full_name or 'Agent'}, calling from CallMira regarding your inquiry."
        qual_questions = [
            "1. Are you the homeowner?",
            "2. What is your average monthly electricity bill?",
            "3. Is your roof shaded or unshaded?"
        ]
        req_info = ["Homeowner verification", "Electricity bill amount", "Roof type"]
        trans_rules = "Transfer to Sales Representative if qualified."

        if campaign and campaign.schedule and isinstance(campaign.schedule, dict):
            if campaign.schedule.get("script"):
                script_text = campaign.schedule["script"].replace("{{name}}", lead_name)
            if campaign.schedule.get("qualification_questions"):
                qual_questions = campaign.schedule["qualification_questions"]
            if campaign.schedule.get("required_info"):
                req_info = campaign.schedule["required_info"]
            if campaign.schedule.get("transfer_rules"):
                trans_rules = campaign.schedule["transfer_rules"]

        client_name = ""
        client_phone = ""
        client_company = ""
        client_id = None
        if campaign:
            client_id = campaign.client_id
            if campaign.client_id:
                client_obj = await db.get(Client, campaign.client_id)
                if client_obj:
                    client_name = client_obj.name or ""
                    client_company = client_obj.company_name or client_obj.name or ""
                    client_phone = client_obj.contact_number or ""

        return {
            "lead": {
                "id": next_lead.id,
                "lead_name": lead_name,
                "first_name": next_lead.first_name or "",
                "last_name": next_lead.last_name or "",
                "phone": next_lead.phone or "",
                "email": next_lead.email or "",
                "company": next_lead.company or "",
                "requirement": next_lead.requirement or "Interested in campaign offering",
                "qualification_status": next_lead.qualification_status or "UNQUALIFIED",
                "location": next_lead.location or "",
                "status": next_lead.status or "NEW",
            },
            "client": {
                "id": client_id,
                "name": client_name,
                "company": client_company,
                "phone": client_phone,
            },
            "campaign": {
                "id": campaign.id if campaign else "camp-default",
                "name": campaign.name if campaign else "Outbound Campaign",
                "client_id": client_id,
                "client_name": client_name,
                "client_company": client_company,
                "client_phone": client_phone,
                "lead_group_id": campaign.lead_group_id if campaign else None,
                "script": script_text,
                "qualification_questions": qual_questions,
                "required_info": req_info,
                "transfer_rules": trans_rules,
            },
            "total_count": total_count,
        }
    except Exception as exc:
        logger.exception(f"Error in get_next_dial_lead: {exc}")
        await db.rollback()
        return {
            "lead": None,
            "campaign": None,
            "total_count": 0,
            "remaining_count": 0,
            "message": "No more customers available."
        }


# ---------------------------------------------------------------------------
# 14. Campaign Script
# ---------------------------------------------------------------------------

@router.get("/campaigns/{id}/script")
async def get_campaign_script(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.campaigns import get_campaign_script as get_camp_script_impl
    return await get_camp_script_impl(id=id, user=user, db=db)

# ---------------------------------------------------------------------------
# Silent Call Barging
# ---------------------------------------------------------------------------

@router.post("/calls/{id}/listen")
async def dashboard_listen_call(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_barge import start_listen
    return await start_listen(call_id=id, user=user, db=db)


@router.post("/calls/{id}/stop-listen")
async def dashboard_stop_listen(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_barge import stop_listen
    return await stop_listen(call_id=id, user=user, db=db)


@router.post("/calls/{id}/barge")
async def barge_in_call(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_barge import start_barge
    return await start_barge(call_id=id, user=user, db=db)


@router.post("/calls/{id}/leave-barge")
async def leave_barge(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_barge import end_barge
    return await end_barge(call_id=id, user=user, db=db)


@router.get("/calls/active")
async def get_active_calls(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    role = (user.role or "").strip().upper()
    if role not in ("ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER", "AGENT", "VOICE_AGENT"):
        raise HTTPException(status_code=403, detail="Not authorized")
    
    from datetime import datetime, timezone
    from app.db.models import Campaign, Lead, Call
    from sqlalchemy import select, or_, desc

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    today_start = datetime.combine(now.date(), datetime.min.time())

    user_biz = user.business_id
    if not user_biz and role != "SUPER_ADMIN":
        return []

    # 1. Clean up any stale/orphaned calls from previous days or older than 4 hours
    stale_stmt = select(Call).where(
        Call.ended_at.is_(None),
        Call.status.notin_(["COMPLETED", "FAILED", "BUSY", "NO_ANSWER", "CANCELED"]),
        or_(
            Call.created_at < today_start,
            Call.initiated_at < today_start
        )
    )
    if user_biz:
        stale_stmt = stale_stmt.where(Call.business_id == user_biz)
    stale_calls = (await db.execute(stale_stmt)).scalars().all()
    if stale_calls:
        for sc in stale_calls:
            sc.status = "COMPLETED"
            sc.ended_at = sc.created_at or now
        try:
            await db.commit()
        except Exception:
            await db.rollback()

    # 2. Fetch ALL active calls today (regardless of campaign status)
    active_statuses = [
        "IN_PROGRESS", "AI_HANDLING", "HUMAN_HANDLING",
        "CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING", "QUALIFIED",
        "TRANSFER_REQUESTED", "QUEUED", "PARKED", "CALL_PARKED",
        "RINGING", "TALKING", "ACTIVE", "CONNECTED", "LIVE", "CALLING", "BRIDGED",
        "DIALING", "IN_CALL", "RESERVED",
        "initiating", "ringing", "answered", "in_progress"
    ]

    all_camp_stmt = select(Campaign)
    if user_biz:
        all_camp_stmt = all_camp_stmt.where(Campaign.business_id == user_biz)
    all_camps = (await db.scalars(all_camp_stmt)).all()
    all_camp_map = {c.id: c for c in all_camps}

    call_query = (
        select(Call, Lead, Campaign)
        .outerjoin(Lead, Call.lead_id == Lead.id)
        .outerjoin(Campaign, Call.campaign_id == Campaign.id)
        .where(
            Call.ended_at.is_(None),
            Call.status.in_(active_statuses),
            or_(Call.initiated_at >= today_start, Call.created_at >= today_start),
        )
    )
    if user_biz:
        call_query = call_query.where(Call.business_id == user_biz)
    call_query = call_query.order_by(desc(func.coalesce(Call.initiated_at, Call.created_at)))
    rows = (await db.execute(call_query)).all()

    active_calls = []
    for call, lead, campaign in rows:
        # Resolve campaign name: prefer the joined campaign, then lookup from map, then fallback
        target_camp = campaign or all_camp_map.get(call.campaign_id)
        campaign_name = target_camp.name if target_camp else "Direct / Voice Agent Call"
        campaign_id = target_camp.id if target_camp else None

        lead_name = (
            f"{lead.first_name or ''} {lead.last_name or ''}".strip()
            if lead
            else (call.client_name or call.to_number or "Active Lead")
        )

        last_msg = "Call in progress..."
        if call.transcript and call.transcript.strip():
            lines = [line.strip() for line in call.transcript.split("\n") if line.strip()]
            if lines:
                last_msg = lines[-1]

        active_calls.append({
            "call_id": call.id,
            "campaign_id": campaign_id,
            "campaign_name": campaign_name,
            "lead_name": lead_name,
            "agent_id": call.agent_id,
            "status": call.status,
            "duration_seconds": call.duration_seconds or 0,
            "last_message": last_msg,
            "direction": call.direction or "OUTBOUND",
        })

    return active_calls


@router.get("/timeline")
async def get_agent_timeline(
    date: str = Query(None, description="YYYY-MM-DD format"),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import AuditLog, Call, Lead, Expert
    from sqlalchemy import select, or_

    def _fmt_duration(secs):
        if not secs or secs <= 0:
            return None
        m = secs // 60
        s = secs % 60
        return f"{m}m {s}s" if m > 0 else f"{s}s"

    try:
        # Calculate UTC bounds for the requested day in Asia/Kolkata timezone
        start_of_day, end_of_day, target_date = get_ist_day_bounds_in_utc(date)
        today_ist = get_current_ist_time().date()

        # Collect agent identifiers
        agent_ids = [user.id]
        expert = await db.scalar(
            select(Expert).where((Expert.user_id == user.id) | (Expert.email == user.email))
        )
        if expert and expert.id not in agent_ids:
            agent_ids.append(expert.id)

        # Fetch status changes from AuditLog
        audit_stmt = (
            select(AuditLog)
            .where(
                AuditLog.user_id == user.id,
                AuditLog.action.in_(["AGENT_STATUS_CHANGED", "USER_LOGGED_IN", "USER_LOGGED_OUT"]),
                AuditLog.created_at >= start_of_day,
                AuditLog.created_at <= end_of_day
            )
            .order_by(AuditLog.created_at.asc())
        )
        audits = list((await db.execute(audit_stmt)).scalars().all())

        # Fetch calls assigned to this agent
        call_conds = [
            or_(Call.agent_id.in_(agent_ids), Call.agent_id == user.id, Call.user_id == user.id),
            Call.created_at >= start_of_day,
            Call.created_at <= end_of_day
        ]
        if user.business_id:
            call_conds.append(Call.business_id == user.business_id)
        call_stmt = (
            select(Call, Lead)
            .outerjoin(Lead, Call.lead_id == Lead.id)
            .where(*call_conds)
            .order_by(Call.created_at.asc())
        )
        call_rows = list((await db.execute(call_stmt)).all())

        activities = []

        # If user logged in today but no audit record existed previously, ensure an initial login event
        if target_date == today_ist and not any(a.action == "USER_LOGGED_IN" for a in audits):
            user_created_dt = user.created_at
            login_time = user_created_dt if user_created_dt and user_created_dt >= start_of_day else datetime.now(timezone.utc).replace(tzinfo=None)
            activities.append({
                "id": f"init_login_{user.id}",
                "type": "login",
                "label": "Agent Logged In (Session Start)",
                "timestamp": to_utc_iso(login_time),
                "duration": None
            })

        for a in audits:
            a_iso = to_utc_iso(a.created_at)
            if a.action == "AGENT_STATUS_CHANGED":
                new_status = (a.details or {}).get("to_status", user.status or "AVAILABLE")
                if new_status == "COFFEE_BREAK":
                    type_str = "coffee_break"
                    label = "Coffee Break Started"
                elif new_status == "LUNCH_BREAK":
                    type_str = "lunch_break"
                    label = "Lunch Break Started"
                elif new_status in ("PAUSED", "ON BREAK"):
                    type_str = "pause"
                    label = "Agent Paused (Break Taken)"
                elif new_status == "AVAILABLE":
                    type_str = "resume"
                    label = "Agent Resumed (Ready for Calls)"
                elif new_status == "ON CALL":
                    type_str = "call"
                    label = "Agent Connected to Call"
                elif new_status == "OFFLINE":
                    type_str = "logout"
                    label = "Agent Status: Offline"
                else:
                    type_str = "pause" if "BREAK" in new_status or "PAUSE" in new_status else "status"
                    label = f"Status Changed: {new_status}"

                activities.append({
                    "id": a.id,
                    "type": type_str,
                    "label": label,
                    "timestamp": a_iso,
                    "duration": None
                })
            elif a.action == "USER_LOGGED_IN":
                activities.append({
                    "id": a.id,
                    "type": "login",
                    "label": "Agent Logged In",
                    "timestamp": a_iso,
                    "duration": None
                })
            elif a.action == "USER_LOGGED_OUT":
                activities.append({
                    "id": a.id,
                    "type": "logout",
                    "label": "Agent Logged Out",
                    "timestamp": a_iso,
                    "duration": None
                })

        for row in call_rows:
            c = row[0]
            lead = row[1] if len(row) > 1 else None
            cust_name = f"{lead.first_name} {lead.last_name}".strip() if lead else (c.to_number or "Customer")
            if not cust_name: cust_name = "Customer"

            c_iso = to_utc_iso(c.created_at)
            activities.append({
                "id": f"{c.id}_start",
                "type": "call",
                "label": f"Call Started: {cust_name} ({c.to_number or 'Direct'})",
                "timestamp": c_iso,
                "duration": _fmt_duration(c.duration_seconds)
            })
            
            if c.status not in ("QUEUED", "RINGING", "IN_PROGRESS", "CLIENT_ADDING", "CONFERENCE_3WAY_ACTIVE", "HUMAN_HANDLING", "AI_HANDLING", "TRANSFER_REQUESTED"):
                end_iso = to_utc_iso(c.ended_at or c.updated_at or c.created_at) or c_iso
                activities.append({
                    "id": f"{c.id}_end",
                    "type": "dispose",
                    "label": f"Call Ended: {cust_name} — {c.call_result or c.status or 'Completed'}",
                    "timestamp": end_iso,
                    "duration": _fmt_duration(c.duration_seconds)
                })

        # Sort all by timestamp
        activities.sort(key=lambda x: x["timestamp"] or "")

        # Calculate relative interval durations between sequential events
        for i in range(len(activities) - 1):
            if not activities[i].get("duration"):
                try:
                    s1 = activities[i]["timestamp"].replace("Z", "+00:00")
                    s2 = activities[i+1]["timestamp"].replace("Z", "+00:00")
                    t1 = datetime.fromisoformat(s1)
                    t2 = datetime.fromisoformat(s2)
                    diff_sec = int((t2 - t1).total_seconds())
                    if diff_sec > 0:
                        activities[i]["duration"] = _fmt_duration(diff_sec)
                except Exception:
                    pass

        return activities
    except Exception as exc:
        logger.exception(f"Error generating agent timeline: {exc}")
        return []



# ---------------------------------------------------------------------------
# Daily Login Report
# ---------------------------------------------------------------------------

@router.get("/dashboard/daily-report")
async def get_daily_report(
    date: str = Query(None, description="Date in YYYY-MM-DD format"),
    agent_id: str = Query(None, description="Optional agent_id filter for Supervisors/Admins"),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db)
):
    from app.db.models import AuditLog, Expert, Call, Lead, User
    from sqlalchemy import or_

    try:
        # Calculate UTC bounds for the requested day in Asia/Kolkata timezone
        start_of_day, end_of_day, target_date = get_ist_day_bounds_in_utc(date)
        today_ist = get_current_ist_time().date()

        is_supervisor_or_admin = user.role in ("SUPERVISOR", "ADMIN", "SUPER_ADMIN", "BUSINESS_OWNER")
        
        # Available agents for supervisor/admin dropdown filter
        available_agents = []
        if is_supervisor_or_admin:
            try:
                if user.business_id:
                    users_stmt = select(User).where(
                        User.business_id == user.business_id,
                        User.role.in_(["VOICE_AGENT", "AGENT", "USER", "SUPERVISOR", "ADMIN"])
                    ).order_by(User.full_name.asc())
                elif user.role == "SUPER_ADMIN":
                    users_stmt = select(User).order_by(User.full_name.asc())
                else:
                    users_stmt = select(User).where(User.id == user.id)
                biz_users = (await db.scalars(users_stmt)).all()
                for u in biz_users:
                    available_agents.append({
                        "id": u.id,
                        "name": u.full_name or u.email,
                        "email": u.email,
                        "role": u.role
                    })
            except Exception as e:
                logger.debug(f"Could not list available agents: {e}")

        # Determine target agent scope
        target_user_ids = []
        target_agent_ids = []
        agent_names_map = {}

        if is_supervisor_or_admin:
            if agent_id and agent_id.strip() and agent_id != "ALL":
                # Specific agent selected by supervisor
                target_user = await db.get(User, agent_id)
                if not target_user:
                    expert_target = await db.get(Expert, agent_id)
                    if expert_target:
                        target_user = await db.scalar(select(User).where(or_(User.id == expert_target.user_id, User.email == expert_target.email)))
                
                if not target_user:
                    raise HTTPException(status_code=404, detail="Voice Agent not found")
                    
                if user.role != "SUPER_ADMIN" and user.business_id and target_user.business_id and target_user.business_id != user.business_id:
                    raise HTTPException(status_code=403, detail="Forbidden: You do not have permission to view this Voice Agent's report")
                    
                target_user_ids.append(target_user.id)
                target_agent_ids.append(target_user.id)
                agent_names_map[target_user.id] = target_user.full_name or target_user.email
                
                expert_obj = await db.scalar(select(Expert).where(or_(Expert.user_id == target_user.id, Expert.email == target_user.email)))
                if expert_obj:
                    target_agent_ids.append(expert_obj.id)
                    agent_names_map[expert_obj.id] = target_user.full_name or target_user.email
            else:
                # All agents managed by supervisor
                for ag in available_agents:
                    target_user_ids.append(ag["id"])
                    target_agent_ids.append(ag["id"])
                    agent_names_map[ag["id"]] = ag["name"]
                # Also include experts in business
                if user.business_id:
                    experts_list = (await db.scalars(select(Expert).where(Expert.business_id == user.business_id))).all()
                    for exp in experts_list:
                        if exp.id not in target_agent_ids:
                            target_agent_ids.append(exp.id)
                        agent_names_map[exp.id] = exp.name or agent_names_map.get(exp.user_id, "Voice Agent")
        else:
            # Regular voice agent (restricted to own ID)
            if agent_id and agent_id.strip() and agent_id != "ALL" and agent_id != user.id:
                raise HTTPException(status_code=403, detail="Forbidden: You can only view your own report")
                
            target_user_ids.append(user.id)
            target_agent_ids.append(user.id)
            agent_names_map[user.id] = user.full_name or user.email
            expert_obj = await db.scalar(select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email)))
            if expert_obj and expert_obj.id not in target_agent_ids:
                target_agent_ids.append(expert_obj.id)
                agent_names_map[expert_obj.id] = user.full_name or user.email

        # 1. Fetch Audit Logs for Login/Logout/Break tracking
        audits = []
        try:
            if target_user_ids:
                audit_stmt = (
                    select(AuditLog)
                    .where(
                        AuditLog.user_id.in_(target_user_ids),
                        AuditLog.created_at >= start_of_day,
                        AuditLog.created_at <= end_of_day
                    )
                    .order_by(AuditLog.created_at.asc())
                )
                audits = list((await db.execute(audit_stmt)).scalars().all())
        except Exception as err:
            logger.warning(f"Could not load audit logs for daily report: {err}")

        first_login_time = None
        logout_time = None
        audit_login_time = None
        
        coffee_break_seconds = 0
        lunch_break_seconds = 0
        current_break_type = None
        break_start = None
        
        for a in audits:
            a_time = a.created_at
            if not a_time:
                continue
            if a.action == "USER_LOGGED_IN":
                if not audit_login_time or a_time < audit_login_time:
                    audit_login_time = a_time
            elif a.action == "USER_LOGGED_OUT":
                if not logout_time or a_time > logout_time:
                    logout_time = a_time
            elif a.action == "AGENT_STATUS_CHANGED":
                details = a.details or {}
                new_status = details.get("to_status", "")
                if new_status in ("COFFEE_BREAK", "LUNCH_BREAK") and break_start is None:
                    current_break_type = new_status
                    break_start = a_time
                elif new_status == "AVAILABLE" and break_start is not None:
                    duration = int((a_time - break_start).total_seconds())
                    if current_break_type == "COFFEE_BREAK":
                        coffee_break_seconds += max(0, duration)
                    elif current_break_type == "LUNCH_BREAK":
                        lunch_break_seconds += max(0, duration)
                    break_start = None
                    current_break_type = None

        if break_start:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            end_time = now_utc if target_date == today_ist else end_of_day
            duration = int((end_time - break_start).total_seconds())
            if current_break_type == "COFFEE_BREAK":
                coffee_break_seconds += max(0, duration)
            elif current_break_type == "LUNCH_BREAK":
                lunch_break_seconds += max(0, duration)

        if not audit_login_time and audits:
            audit_login_time = audits[0].created_at

        first_login_time = audit_login_time

        # 2. Get calls for the day
        calls_conditions = [
            Call.created_at >= start_of_day,
            Call.created_at <= end_of_day
        ]
        if user.business_id:
            calls_conditions.append(Call.business_id == user.business_id)
        elif user.role != "SUPER_ADMIN":
            calls_conditions.append(Call.agent_id == user.id)

        if agent_id and agent_id.strip() and agent_id != "ALL":
            if target_agent_ids:
                calls_conditions.append(Call.agent_id.in_(target_agent_ids))
            else:
                calls_conditions.append(Call.agent_id == agent_id)
        elif not is_supervisor_or_admin:
            calls_conditions.append(Call.agent_id == user.id)

        calls_stmt = (
            select(Call, Lead)
            .outerjoin(Lead, Call.lead_id == Lead.id)
            .where(*calls_conditions)
            .order_by(Call.created_at.desc())
        )
        results = list((await db.execute(calls_stmt)).all())
        
        total_calls = len(results)
        completed_calls = sum(1 for row in results if (row[0].status == "COMPLETED" or (row[0].duration_seconds or 0) > 0))
        total_talk_time = sum((row[0].duration_seconds or 0) for row in results)
        
        if not first_login_time and total_calls > 0:
            first_login_time = results[-1][0].created_at
        elif not first_login_time and target_date == today_ist:
            first_login_time = datetime.now(timezone.utc).replace(tzinfo=None)
            
        total_break_time_seconds = 0
        if first_login_time:
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            end_time = logout_time if logout_time else now_utc
            if target_date != today_ist and not logout_time:
                end_time = end_of_day
                
            total_session_seconds = int((end_time - first_login_time).total_seconds())
            if total_session_seconds > 0:
                total_break_time_seconds = max(0, total_session_seconds - total_talk_time)

        call_logs = []
        for row in results:
            call_obj = row[0]
            lead_obj = row[1] if len(row) > 1 else None
            
            customer_name = f"{lead_obj.first_name} {lead_obj.last_name}".strip() if lead_obj else "Customer"
            if not customer_name:
                customer_name = "Unknown Customer"
            
            customer_phone = call_obj.to_number or (lead_obj.phone if lead_obj else "")
            agent_disp_name = agent_names_map.get(call_obj.agent_id) or "Voice Agent"
            
            dur_sec = call_obj.duration_seconds or 0
            rec_url = call_obj.recording_url or ""
            if not rec_url:
                clean_status = (call_obj.status or "").upper().strip()
                if clean_status not in ("FAILED", "BUSY", "NO_ANSWER", "NO-ANSWER", "MISSED", "CANCELED", "CANCELLED", "REJECTED") and (dur_sec > 0 or call_obj.transcript):
                    from app.services.recording_service import ensure_call_recording
                    rec_url = ensure_call_recording(call_obj.id, duration_sec=dur_sec or 15)
                    if rec_url:
                        call_obj.recording_url = rec_url
                        db.add(call_obj)

            call_logs.append({
                "id": call_obj.id,
                "agent_id": call_obj.agent_id,
                "agent_name": agent_disp_name,
                "customer_name": customer_name,
                "customer_phone": customer_phone,
                "start_time": to_ist_time_str(call_obj.created_at),
                "connected_time": to_ist_time_str(call_obj.connected_at) if call_obj.connected_at else "—",
                "end_time": to_ist_time_str(call_obj.ended_at) if call_obj.ended_at else "—",
                "duration_seconds": dur_sec,
                "formatted_duration": f"{dur_sec // 60:02d}:{dur_sec % 60:02d}",
                "status": call_obj.status or "COMPLETED",
                "outcome": call_obj.call_result or ("Connected" if dur_sec > 0 else "No Answer"),
                "recording_url": rec_url,
                "has_recording": True,
                "record_id": f"REC-{call_obj.id[:8].upper()}",
            })

        return {
            "summary": {
                "first_login_time": to_utc_iso(first_login_time),
                "logout_time": to_utc_iso(logout_time),
                "total_calls": total_calls,
                "completed_calls": completed_calls,
                "total_talk_time_seconds": total_talk_time,
                "total_break_time_seconds": total_break_time_seconds,
                "coffee_break_seconds": coffee_break_seconds,
                "lunch_break_seconds": lunch_break_seconds
            },
            "calls": call_logs,
            "available_agents": available_agents
        }
    except HTTPException:
        await db.rollback()
        raise
    except Exception as exc:
        await db.rollback()
        logger.exception(f"Error generating daily report for user {user.id}: {exc}")
        return {
            "summary": {
                "first_login_time": None,
                "logout_time": None,
                "total_calls": 0,
                "completed_calls": 0,
                "total_talk_time_seconds": 0,
                "total_break_time_seconds": 0,
                "coffee_break_seconds": 0,
                "lunch_break_seconds": 0
            },
            "calls": [],
            "available_agents": []
        }


@router.get("/leads/export")
async def export_voice_agent_leads_csv(
    format: str = Query("csv"),
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Export all Voice Agent leads to CSV or Excel (XLSX) with Call Status and Disposal Status columns."""
    try:
        biz_id = user.business_id
        if not biz_id and (user.role or "").upper() != "SUPER_ADMIN":
            return Response(content="", media_type="text/csv")

        stmt = select(Lead)
        if biz_id:
            stmt = stmt.where(Lead.business_id == biz_id)
        stmt = stmt.order_by(desc(Lead.created_at))

        leads = (await db.execute(stmt)).scalars().all()

        headers = [
            "ID", "First Name", "Last Name", "Phone", "Email", 
            "Company", "Job Title", "Requirement", "Budget", "Timeline", 
            "Location", "Call Status", "Disposal Status", "Lead Status", "Created At"
        ]
        rows = [headers]

        for lead in leads:
            full_first = lead.first_name or ""
            full_last = lead.last_name or ""
            call_status = lead.call_status or "Not Called"
            disposal_status = lead.call_result or "—"
            created_str = lead.created_at.strftime("%Y-%m-%d %H:%M:%S") if lead.created_at else ""

            rows.append([
                str(lead.id or ""),
                full_first,
                full_last,
                str(lead.phone or ""),
                str(lead.email or ""),
                str(lead.company or ""),
                str(lead.job_title or ""),
                str(lead.requirement or ""),
                str(lead.budget or ""),
                str(lead.timeline or ""),
                str(lead.location or ""),
                call_status,
                disposal_status,
                str(lead.status or "NEW"),
                created_str,
            ])

        fmt = format.lower()
        if fmt in ("xlsx", "excel"):
            import openpyxl
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "Leads"
            for row in rows:
                ws.append(row)
            output = io.BytesIO()
            wb.save(output)
            xlsx_data = output.getvalue()
            return Response(
                content=xlsx_data,
                media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                headers={"Content-Disposition": 'attachment; filename="voice_agent_leads_latest.xlsx"'}
            )

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerows(rows)
        csv_data = output.getvalue()

        return Response(
            content=csv_data,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="voice_agent_leads_latest.csv"'}
        )
    except Exception as exc:
        logger.exception(f"Error exporting voice agent leads: {exc}")
        raise HTTPException(status_code=500, detail="Failed to export leads")
