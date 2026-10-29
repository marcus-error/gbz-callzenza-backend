from fastapi import APIRouter, Depends, HTTPException, status, Query
from loguru import logger
from sqlalchemy import select, func, or_, desc
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any
import uuid
import re
import json

from app.core.deps import get_current_business_id, get_current_business_id_optional, get_current_db_user, require_supervisor_or_admin
from app.db.database import get_db
from app.db.models import Business, User, UserGroup, UserGroupMember, InboundNumber, AIAgent, Call, AuditLog, CallTransfer, Expert

router = APIRouter(prefix="/api/inbound", tags=["inbound"])


# Schemas
class InboundCallSummary(BaseModel):
    id: str
    call_sid: str
    from_number: str
    to_number: str
    status: str
    duration_seconds: int
    client_name: str = ""
    agent_name: str = ""
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InboundCreatePayload(BaseModel):
    phone_number: str
    name: str
    description: Optional[str] = ""
    status: Optional[str] = "ACTIVE"
    routing_type: Optional[str] = "VOICE_AGENT"  # VOICE_AGENT, HUMAN_AGENT, USER_GROUP, QUEUE, IVR
    assigned_agent_id: Optional[str] = None
    assigned_ai_agent_id: Optional[str] = None
    assigned_group_id: Optional[str] = None
    working_hours: Optional[Dict[str, Any]] = None
    fallback_action: Optional[str] = "VOICEMAIL"


class InboundUpdatePayload(BaseModel):
    phone_number: Optional[str] = None
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[str] = None
    routing_type: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    assigned_ai_agent_id: Optional[str] = None
    assigned_group_id: Optional[str] = None
    working_hours: Optional[Dict[str, Any]] = None
    fallback_action: Optional[str] = None


class InboundStatusTogglePayload(BaseModel):
    status: str  # ACTIVE or INACTIVE


class SimulateInboundCallPayload(BaseModel):
    caller_name: Optional[str] = "Vikram Sharma"
    caller_phone: Optional[str] = "+91 98765 43210"
    to_number: Optional[str] = "+1 800 555 0199"
    inbound_number_id: Optional[str] = None
    agent_type: Optional[str] = "AI"  # "AI" or "VOICE"
    campaign_id: Optional[str] = None
    notes: Optional[str] = "Inbound customer inquiry"


class RouteInboundCallPayload(BaseModel):
    agent_id: str



class InboundNumberOut(BaseModel):
    id: str
    business_id: str
    phone_number: str
    name: str
    description: str
    status: str
    routing_type: str
    assigned_agent_id: Optional[str] = None
    assigned_agent_name: Optional[str] = None
    assigned_ai_agent_id: Optional[str] = None
    assigned_ai_agent_name: Optional[str] = None
    assigned_group_id: Optional[str] = None
    assigned_group_name: Optional[str] = None
    working_hours: Dict[str, Any] = {}
    fallback_action: str = "VOICEMAIL"
    calls_today: int = 0
    total_calls: int = 0
    last_call_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InboundDetailOut(InboundNumberOut):
    recent_calls: List[InboundCallSummary] = []


class InboundStatsOut(BaseModel):
    total_numbers: int
    active_numbers: int
    calls_today: int
    total_inbound_calls: int


def _clean_phone(phone: str) -> str:
    # Retain leading + if present, but strip other formatting characters
    cleaned = re.sub(r"[^\d+]", "", phone.strip())
    return cleaned


async def _sync_transferred_inbound_agents(db: AsyncSession, business_id: str):
    """Auto-provision InboundNumber entries for all inbound agents who received transfers."""
    try:
        from loguru import logger
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        
        # 1. Query transfers scoped to this business
        transfers_stmt = select(CallTransfer).order_by(CallTransfer.created_at.desc())
        if business_id:
            transfers_stmt = transfers_stmt.where(CallTransfer.business_id == business_id)
        all_transfers = (await db.scalars(transfers_stmt)).all()

        agent_transfers_map = {}
        for t in all_transfers:
            if t.to_agent_id:
                agent_transfers_map.setdefault(t.to_agent_id, []).append(t)

        for target_id, t_list in agent_transfers_map.items():
            # Resolve user & expert
            u = await db.get(User, target_id)
            exp = (await db.scalars(select(Expert).where(or_(Expert.user_id == target_id, Expert.id == target_id)))).first()
            if not u and exp and exp.user_id:
                u = await db.get(User, exp.user_id)

            effective_agent_id = u.id if u else (exp.user_id if exp and exp.user_id else target_id)
            target_name = (u.full_name if u else None) or (exp.name if exp else None) or "Inbound Agent"
            target_email = (u.email if u else None) or (exp.email if exp else "")

            # Check if InboundNumber already exists for this agent
            existing_inb = (await db.scalars(
                select(InboundNumber).where(
                    or_(
                        InboundNumber.assigned_agent_id == effective_agent_id,
                        InboundNumber.assigned_agent_id == target_id
                    )
                )
            )).first()

            # Find best phone number from the transferred calls
            phone_num = "+18392615369"
            if t_list:
                for t in t_list:
                    c = await db.get(Call, t.call_id)
                    if c and (c.to_number or c.client_phone or c.from_number):
                        candidate = c.to_number or c.client_phone or c.from_number
                        if candidate and candidate.strip():
                            phone_num = candidate.strip()
                            break

            biz_target = business_id or (u.business_id if u and u.business_id else None)
            if not biz_target or biz_target == "default":
                biz_row = (await db.execute(select(Business))).scalars().first()
                biz_target = biz_row.id if biz_row else "c3dce06c-8330-4724-ba33-b5282b3c3596"

            if not existing_inb:
                new_inb = InboundNumber(
                    id=str(uuid.uuid4()),
                    business_id=biz_target,
                    phone_number=phone_num,
                    name=f"Inbound Agent - {target_name}",
                    description=f"Transferred call routing to Inbound Agent {target_name} ({target_email})",
                    status="ACTIVE",
                    routing_type="HUMAN_AGENT",
                    assigned_agent_id=effective_agent_id,
                    fallback_action="VOICEMAIL",
                    working_hours={"start": "09:00", "end": "18:00", "days": ["MON", "TUE", "WED", "THU", "FRI"]},
                    created_at=now,
                    updated_at=now,
                )
                db.add(new_inb)
            else:
                if not existing_inb.assigned_agent_id:
                    existing_inb.assigned_agent_id = effective_agent_id
                if not existing_inb.business_id or existing_inb.business_id == "default":
                    existing_inb.business_id = biz_target
                existing_inb.status = "ACTIVE"
                existing_inb.updated_at = now
                db.add(existing_inb)

        await db.commit()
    except Exception as e:
        logger.warning(f"Error syncing transferred inbound agents: {e}")
        await db.rollback()


@router.get("/stats/summary", response_model=InboundStatsOut)
async def get_inbound_stats(
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor_or_admin),
):
    await _sync_transferred_inbound_agents(db, business_id)

    # Total numbers
    total_q = select(func.count(InboundNumber.id))
    if (user.role or "").strip().upper() != "SUPER_ADMIN" and business_id:
        total_q = total_q.where(InboundNumber.business_id == business_id)
    total_numbers = (await db.scalar(total_q)) or 0

    # Active numbers
    active_q = select(func.count(InboundNumber.id)).where(InboundNumber.status == "ACTIVE")
    if (user.role or "").strip().upper() != "SUPER_ADMIN" and business_id:
        active_q = active_q.where(InboundNumber.business_id == business_id)
    active_numbers = (await db.scalar(active_q)) or 0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)

    # Count calls today and total inbound/transferred calls
    transfer_call_ids_subq = select(CallTransfer.call_id)
    inbound_call_cond = or_(
        Call.direction == "INBOUND",
        Call.id.in_(transfer_call_ids_subq)
    )

    calls_today_q = select(func.count(Call.id)).where(
        inbound_call_cond,
        Call.created_at >= today_start
    )
    total_calls_q = select(func.count(Call.id)).where(inbound_call_cond)
    if (user.role or "").strip().upper() != "SUPER_ADMIN" and business_id:
        calls_today_q = calls_today_q.where(Call.business_id == business_id)
        total_calls_q = total_calls_q.where(Call.business_id == business_id)

    calls_today = (await db.scalar(calls_today_q)) or 0
    total_inbound_calls = (await db.scalar(total_calls_q)) or 0

    return InboundStatsOut(
        total_numbers=total_numbers,
        active_numbers=active_numbers,
        calls_today=calls_today,
        total_inbound_calls=total_inbound_calls,
    )


@router.get("", response_model=List[InboundNumberOut])
async def list_inbound_numbers(
    status_filter: Optional[str] = None,
    routing_filter: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor_or_admin),
):
    await _sync_transferred_inbound_agents(db, business_id)

    query = select(InboundNumber)
    if (user.role or "").strip().upper() != "SUPER_ADMIN" and business_id:
        query = query.where(InboundNumber.business_id == business_id)
    if status_filter:
        query = query.where(InboundNumber.status == status_filter.upper())
    if routing_filter:
        query = query.where(InboundNumber.routing_type == routing_filter.upper())
    query = query.order_by(InboundNumber.created_at.desc())

    inbound_list = (await db.scalars(query)).all()
    results = []

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)

    for inb in inbound_list:
        # Resolve Assigned Agent Name
        agent_name = None
        if inb.assigned_agent_id:
            agent = await db.get(User, inb.assigned_agent_id)
            if agent:
                agent_name = agent.full_name or agent.email
            else:
                exp = (await db.scalars(select(Expert).where(or_(Expert.user_id == inb.assigned_agent_id, Expert.id == inb.assigned_agent_id)))).first()
                if exp:
                    agent_name = exp.name or exp.email

        # Resolve Assigned AI Agent Name
        ai_agent_name = None
        if inb.assigned_ai_agent_id:
            ai_agent = await db.get(AIAgent, inb.assigned_ai_agent_id)
            if ai_agent:
                ai_agent_name = ai_agent.name

        # Resolve Assigned User Group Name
        group_name = None
        if inb.assigned_group_id:
            grp = await db.get(UserGroup, inb.assigned_group_id)
            if grp:
                group_name = grp.name

        # Calculate calls today & total calls & last call
        phone = inb.phone_number
        agent_id = inb.assigned_agent_id

        call_conds = []
        if phone:
            call_conds.append(Call.to_number == phone)
            call_conds.append(Call.client_phone == phone)
        if agent_id:
            transfer_call_ids_q = select(CallTransfer.call_id).where(
                or_(CallTransfer.to_agent_id == agent_id, CallTransfer.from_agent_id == agent_id)
            )
            call_conds.append(Call.id.in_(transfer_call_ids_q))
            call_conds.append(Call.agent_id == agent_id)

        if not call_conds:
            call_conds = [Call.direction == "INBOUND"]

        calls_today_q = select(func.count(Call.id)).where(
            or_(*call_conds),
            Call.created_at >= today_start
        )
        c_today = (await db.scalar(calls_today_q)) or 0

        total_calls_q = select(func.count(Call.id)).where(or_(*call_conds))
        t_calls = (await db.scalar(total_calls_q)) or 0

        last_call_q = select(Call.created_at).where(or_(*call_conds)).order_by(Call.created_at.desc()).limit(1)
        last_call_at = await db.scalar(last_call_q)

        results.append(InboundNumberOut(
            id=inb.id,
            business_id=inb.business_id or "default",
            phone_number=inb.phone_number,
            name=inb.name,
            description=inb.description or "",
            status=inb.status,
            routing_type=inb.routing_type,
            assigned_agent_id=inb.assigned_agent_id,
            assigned_agent_name=agent_name,
            assigned_ai_agent_id=inb.assigned_ai_agent_id,
            assigned_ai_agent_name=ai_agent_name,
            assigned_group_id=inb.assigned_group_id,
            assigned_group_name=group_name,
            working_hours=inb.working_hours or {},
            fallback_action=inb.fallback_action or "VOICEMAIL",
            calls_today=c_today,
            total_calls=t_calls,
            last_call_at=last_call_at,
            created_at=inb.created_at,
            updated_at=inb.updated_at,
        ))

    return results


@router.get("/incoming-calls")
async def get_incoming_calls_list(
    db: AsyncSession = Depends(get_db),
    business_id: Optional[str] = Depends(get_current_business_id_optional),
):
    """
    Returns all active incoming calls requiring monitoring or routing in the organization.
    Supports Type 1 (Human Callback), Type 2 (Normal Inbound), and Type 3 (AI Agent Callback).
    """
    from app.services.inbound_routing_service import (
        inbound_session_registry,
        InboundRoutingType,
        InboundRoutingStatus,
    )
    from app.db.models import Call, Lead, Campaign, User, AIAgent

    active_sessions = await inbound_session_registry.list_active(business_id)
    results = [s.to_dict() for s in active_sessions]

    # If in-memory registry missed any DB active calls (e.g. server restart), augment from DB
    session_call_ids = {s.call_id for s in active_sessions}
    db_calls_stmt = (
        select(Call)
        .where(
            Call.direction == "INBOUND",
            Call.status.in_(["RINGING", "WAITING_FOR_ROUTE", "ROUTE_REQUESTED", "RINGING_AGENT", "TRANSFER_IN_PROGRESS", "IN_PROGRESS"]),
            Call.ended_at.is_(None),
        )
        .order_by(Call.created_at.desc())
        .limit(20)
    )
    if business_id:
        db_calls_stmt = db_calls_stmt.where(Call.business_id == business_id)
    
    db_calls = (await db.scalars(db_calls_stmt)).all()
    for c in db_calls:
        if c.id not in session_call_ids:
            results.append({
                "call_id": c.id,
                "session_id": c.call_sid,
                "organization_id": c.business_id,
                "inbound_phone": c.from_number or c.client_phone,
                "from_number": c.from_number,
                "to_number": c.to_number,
                "routing_type": InboundRoutingType.NORMAL_INBOUND if not c.user_id else InboundRoutingType.LEAD_CALLBACK_HUMAN,
                "inbound_type": InboundRoutingType.NORMAL_INBOUND if not c.user_id else InboundRoutingType.LEAD_CALLBACK_HUMAN,
                "routing_status": c.status,
                "lead_id": c.lead_id,
                "lead_name": c.client_name or "Incoming Customer",
                "caller_name": c.client_name or "Incoming Customer",
                "caller_phone": c.client_phone or c.from_number,
                "campaign_id": c.campaign_id,
                "campaign_name": "N/A",
                "outbound_call_id": None,
                "outbound_call_time": None,
                "outbound_agent_id": c.agent_id,
                "outbound_agent_name": "N/A",
                "outbound_agent_type": "N/A",
                "selected_agent_id": c.user_id,
                "selected_agent_name": None,
                "target_user_id": c.user_id,
                "transfer_status": "NONE",
                "call_status": c.status,
                "status": c.status,
                "waiting_for_agent": c.status in ("WAITING_FOR_ROUTE", "RINGING"),
                "initiated_at": c.initiated_at.isoformat() if c.initiated_at else (c.created_at.isoformat() if c.created_at else ""),
                "created_at": c.created_at.isoformat() if c.created_at else "",
                "is_active": True,
            })

    return {"success": True, "incoming_calls": results}


@router.get("/active-incoming")
async def get_active_incoming_call(
    db: AsyncSession = Depends(get_db),
    business_id: Optional[str] = Depends(get_current_business_id_optional),
):
    """Returns the most recent active incoming call session for UI popups."""
    from app.services.inbound_routing_service import inbound_session_registry
    active_sessions = await inbound_session_registry.list_active(business_id)
    if active_sessions:
        return {"active_call": active_sessions[0].to_dict()}
    
    # Fallback to DB (only genuine active calls within last 45s)
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now_utc - timedelta(seconds=45)
    stmt = (
        select(Call)
        .where(
            Call.direction == "INBOUND",
            Call.status.in_(["RINGING", "WAITING_FOR_ROUTE", "ROUTE_REQUESTED", "RINGING_AGENT"]),
            Call.ended_at.is_(None),
            Call.created_at >= cutoff,
        )
        .order_by(Call.created_at.desc())
    )
    if business_id:
        stmt = stmt.where(Call.business_id == business_id)
    call = (await db.scalars(stmt)).first()
    if not call:
        return {"active_call": None}

    return {
        "active_call": {
            "call_id": call.id,
            "session_id": call.call_sid,
            "organization_id": call.business_id,
            "caller_name": call.client_name or "Incoming Customer",
            "caller_phone": call.client_phone or call.from_number,
            "from_number": call.from_number,
            "to_number": call.to_number,
            "agent_type": call.agent_type or "VOICE",
            "status": call.status,
            "routing_status": call.status,
            "inbound_type": "NORMAL_INBOUND",
            "routing_type": "NORMAL_INBOUND",
            "waiting_for_agent": True,
            "created_at": call.created_at.isoformat() if call.created_at else "",
        }
    }


@router.get("/available-agents")
async def get_available_voice_agents(
    db: AsyncSession = Depends(get_db),
    business_id: Optional[str] = Depends(get_current_business_id_optional),
):
    """
    Returns all Human Voice Agents in the organization along with real-time call availability.
    States: AVAILABLE, RINGING, IN_CALL, BUSY, OFFLINE.
    Only AVAILABLE agents are selectable for live routing.
    """
    from app.services.inbound_routing_service import get_organization_agent_availability
    agents = await get_organization_agent_availability(db, business_id)
    return {"success": True, "agents": agents}


@router.post("/calls/{call_id}/route")
async def route_inbound_call(
    call_id: str,
    payload: RouteInboundCallPayload,
    db: AsyncSession = Depends(get_db),
    business_id: Optional[str] = Depends(get_current_business_id_optional),
):
    """
    Admin Live Transfer / Route endpoint:
    Atomically routes an incoming call in WAITING_FOR_ROUTE to the selected AVAILABLE Human Voice Agent.
    Includes double-route protection and real-time availability verification.
    """
    from app.services.inbound_routing_service import execute_live_route_transfer
    try:
        res = await execute_live_route_transfer(
            call_id=call_id,
            target_agent_id=payload.agent_id,
            db=db,
            business_id=business_id,
        )
        return res
    except PermissionError as pe:
        raise HTTPException(status_code=403, detail=str(pe))
    except ValueError as ve:
        raise HTTPException(status_code=404, detail=str(ve))
    except RuntimeError as re_err:
        err_msg = str(re_err)
        if "CALL_ALREADY_ROUTED" in err_msg:
            raise HTTPException(status_code=409, detail="Call has already been routed to an agent")
        if "CALL_NOT_ACTIVE" in err_msg:
            raise HTTPException(status_code=410, detail="Call is no longer active")
        raise HTTPException(status_code=409, detail=err_msg)
    except Exception as e:
        logger.error(f"[ROUTE_ERROR] call_id={call_id} error={e}")
        raise HTTPException(status_code=500, detail=f"Failed to route call: {e}")


@router.get("/{inbound_id}", response_model=InboundDetailOut)
async def get_inbound_number(
    inbound_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_supervisor_or_admin),
):
    inb = await db.get(InboundNumber, inbound_id)
    if not inb:
        raise HTTPException(status_code=404, detail="Inbound number not found")
    if (user.role or "").strip().upper() != "SUPER_ADMIN" and business_id and inb.business_id not in (business_id, "default", None):
        raise HTTPException(status_code=404, detail="Inbound number not found")

    # Resolve names
    agent_name = None
    if inb.assigned_agent_id:
        agent = await db.get(User, inb.assigned_agent_id)
        if agent:
            agent_name = agent.full_name or agent.email
        else:
            exp = (await db.scalars(select(Expert).where(or_(Expert.user_id == inb.assigned_agent_id, Expert.id == inb.assigned_agent_id)))).first()
            if exp:
                agent_name = exp.name or exp.email

    ai_agent_name = None
    if inb.assigned_ai_agent_id:
        ai_agent = await db.get(AIAgent, inb.assigned_ai_agent_id)
        if ai_agent:
            ai_agent_name = ai_agent.name

    group_name = None
    if inb.assigned_group_id:
        grp = await db.get(UserGroup, inb.assigned_group_id)
        if grp:
            group_name = grp.name

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).replace(tzinfo=None)
    phone = inb.phone_number
    agent_id = inb.assigned_agent_id

    call_conds = []
    if phone:
        call_conds.append(Call.to_number == phone)
        call_conds.append(Call.client_phone == phone)
    if agent_id:
        transfer_call_ids_q = select(CallTransfer.call_id).where(
            or_(CallTransfer.to_agent_id == agent_id, CallTransfer.from_agent_id == agent_id)
        )
        call_conds.append(Call.id.in_(transfer_call_ids_q))
        call_conds.append(Call.agent_id == agent_id)

    if not call_conds:
        call_conds = [Call.direction == "INBOUND"]

    c_today = (await db.scalar(
        select(func.count(Call.id)).where(or_(*call_conds), Call.created_at >= today_start)
    )) or 0

    t_calls = (await db.scalar(
        select(func.count(Call.id)).where(or_(*call_conds))
    )) or 0

    last_call_at = await db.scalar(
        select(Call.created_at).where(or_(*call_conds)).order_by(Call.created_at.desc()).limit(1)
    )

    # Fetch recent 10 inbound calls
    recent_calls_q = (
        select(Call)
        .where(or_(*call_conds))
        .order_by(Call.created_at.desc())
        .limit(10)
    )
    calls_res = (await db.scalars(recent_calls_q)).all()

    recent_call_summaries = []
    for c in calls_res:
        recent_call_summaries.append(InboundCallSummary(
            id=c.id,
            call_sid=c.call_sid or "",
            from_number=c.from_number or "",
            to_number=c.to_number or "",
            status=c.status or "COMPLETED",
            duration_seconds=c.duration_seconds or 0,
            client_name=c.client_name or "",
            agent_name=agent_name or ai_agent_name or group_name or "Assigned Route",
            created_at=c.created_at,
        ))

    return InboundDetailOut(
        id=inb.id,
        business_id=inb.business_id,
        phone_number=inb.phone_number,
        name=inb.name,
        description=inb.description or "",
        status=inb.status,
        routing_type=inb.routing_type,
        assigned_agent_id=inb.assigned_agent_id,
        assigned_agent_name=agent_name,
        assigned_ai_agent_id=inb.assigned_ai_agent_id,
        assigned_ai_agent_name=ai_agent_name,
        assigned_group_id=inb.assigned_group_id,
        assigned_group_name=group_name,
        working_hours=inb.working_hours or {},
        fallback_action=inb.fallback_action or "VOICEMAIL",
        calls_today=c_today,
        total_calls=t_calls,
        last_call_at=last_call_at,
        created_at=inb.created_at,
        updated_at=inb.updated_at,
        recent_calls=recent_call_summaries,
    )


@router.post("", response_model=InboundDetailOut, status_code=status.HTTP_201_CREATED)
async def create_inbound_number(
    payload: InboundCreatePayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    name_clean = payload.name.strip()
    if not name_clean:
        raise HTTPException(status_code=400, detail="Inbound number name is required")

    phone_clean = _clean_phone(payload.phone_number)
    if len(phone_clean) < 6:
        raise HTTPException(status_code=400, detail="Please enter a valid phone number (at least 6 digits)")

    # Check duplicate phone number in business
    existing = await db.scalar(
        select(InboundNumber).where(
            InboundNumber.business_id == business_id,
            InboundNumber.phone_number == phone_clean,
        )
    )
    if existing:
        raise HTTPException(status_code=400, detail=f"Phone number '{phone_clean}' is already configured in your business")

    # Validate routing target based on routing_type
    routing_type = (payload.routing_type or "VOICE_AGENT").upper()
    agent_id = payload.assigned_agent_id
    ai_agent_id = payload.assigned_ai_agent_id
    group_id = payload.assigned_group_id

    if routing_type == "HUMAN_AGENT" and agent_id:
        target_agent = await db.get(User, agent_id)
        if not target_agent:
            raise HTTPException(status_code=400, detail="Assigned agent not found")
    elif routing_type == "VOICE_AGENT" and ai_agent_id:
        target_ai = await db.get(AIAgent, ai_agent_id)
        if not target_ai:
            raise HTTPException(status_code=400, detail="Assigned AI agent not found")
    elif routing_type == "USER_GROUP" and group_id:
        target_grp = await db.get(UserGroup, group_id)
        if not target_grp or target_grp.business_id != business_id:
            raise HTTPException(status_code=400, detail="Assigned user group not found")

    new_inbound = InboundNumber(
        id=str(uuid.uuid4()),
        business_id=business_id,
        phone_number=phone_clean,
        name=name_clean,
        description=(payload.description or "").strip(),
        status=(payload.status or "ACTIVE").upper(),
        routing_type=routing_type,
        assigned_agent_id=agent_id if routing_type == "HUMAN_AGENT" else None,
        assigned_ai_agent_id=ai_agent_id if routing_type == "VOICE_AGENT" else None,
        assigned_group_id=group_id if routing_type == "USER_GROUP" else None,
        working_hours=payload.working_hours or {
            "start": "09:00",
            "end": "18:00",
            "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]
        },
        fallback_action=(payload.fallback_action or "VOICEMAIL").upper(),
    )
    db.add(new_inbound)

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="INBOUND_CREATED",
        entity="inbound_numbers",
        entity_id=new_inbound.id,
        details={
            "name": new_inbound.name,
            "phone_number": new_inbound.phone_number,
            "routing_type": new_inbound.routing_type,
        },
    )
    db.add(audit)

    await db.commit()
    await db.refresh(new_inbound)

    return await get_inbound_number(new_inbound.id, business_id, db, current_user)


@router.put("/{inbound_id}", response_model=InboundDetailOut)
async def update_inbound_number(
    inbound_id: str,
    payload: InboundUpdatePayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    inb = await db.get(InboundNumber, inbound_id)
    if not inb or inb.business_id != business_id:
        raise HTTPException(status_code=404, detail="Inbound number not found")

    if payload.name is not None:
        name_clean = payload.name.strip()
        if not name_clean:
            raise HTTPException(status_code=400, detail="Name cannot be empty")
        inb.name = name_clean

    if payload.phone_number is not None:
        phone_clean = _clean_phone(payload.phone_number)
        if len(phone_clean) < 6:
            raise HTTPException(status_code=400, detail="Invalid phone number format")
        # Check duplicate
        existing = await db.scalar(
            select(InboundNumber).where(
                InboundNumber.business_id == business_id,
                InboundNumber.id != inbound_id,
                InboundNumber.phone_number == phone_clean,
            )
        )
        if existing:
            raise HTTPException(status_code=400, detail=f"Phone number '{phone_clean}' is already in use")
        inb.phone_number = phone_clean

    if payload.description is not None:
        inb.description = payload.description.strip()

    if payload.status is not None:
        inb.status = payload.status.upper()

    if payload.routing_type is not None:
        inb.routing_type = payload.routing_type.upper()

    if payload.assigned_agent_id is not None:
        inb.assigned_agent_id = payload.assigned_agent_id if inb.routing_type == "HUMAN_AGENT" else None

    if payload.assigned_ai_agent_id is not None:
        inb.assigned_ai_agent_id = payload.assigned_ai_agent_id if inb.routing_type == "VOICE_AGENT" else None

    if payload.assigned_group_id is not None:
        inb.assigned_group_id = payload.assigned_group_id if inb.routing_type == "USER_GROUP" else None

    if payload.working_hours is not None:
        inb.working_hours = payload.working_hours

    if payload.fallback_action is not None:
        inb.fallback_action = payload.fallback_action.upper()

    # Audit log
    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="INBOUND_UPDATED",
        entity="inbound_numbers",
        entity_id=inb.id,
        details={"name": inb.name, "phone_number": inb.phone_number, "status": inb.status},
    )
    db.add(audit)

    await db.commit()
    await db.refresh(inb)

    return await get_inbound_number(inb.id, business_id, db, current_user)


@router.patch("/{inbound_id}/status", response_model=InboundNumberOut)
async def toggle_inbound_status(
    inbound_id: str,
    payload: InboundStatusTogglePayload,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    inb = await db.get(InboundNumber, inbound_id)
    if not inb or inb.business_id != business_id:
        raise HTTPException(status_code=404, detail="Inbound number not found")

    new_status = payload.status.upper()
    if new_status not in ("ACTIVE", "INACTIVE"):
        raise HTTPException(status_code=400, detail="Status must be ACTIVE or INACTIVE")

    inb.status = new_status

    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action=f"INBOUND_{new_status}",
        entity="inbound_numbers",
        entity_id=inb.id,
        details={"status": new_status},
    )
    db.add(audit)

    await db.commit()
    await db.refresh(inb)

    return await get_inbound_number(inb.id, business_id, db, current_user)


@router.delete("/{inbound_id}")
async def delete_inbound_number(
    inbound_id: str,
    current_user: User = Depends(require_supervisor_or_admin),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    inb = await db.get(InboundNumber, inbound_id)
    if not inb or inb.business_id != business_id:
        raise HTTPException(status_code=404, detail="Inbound number not found")

    name = inb.name
    phone = inb.phone_number

    await db.delete(inb)

    audit = AuditLog(
        id=str(uuid.uuid4()),
        business_id=business_id,
        user_id=current_user.id,
        action="INBOUND_DELETED",
        entity="inbound_numbers",
        entity_id=inbound_id,
        details={"name": name, "phone_number": phone},
    )
    db.add(audit)

    await db.commit()

    return {"success": True, "message": f"Inbound number '{name}' ({phone}) deleted successfully"}


@router.get("/{inbound_id}/calls", response_model=List[InboundCallSummary])
async def list_inbound_calls(
    inbound_id: str,
    limit: int = Query(25, ge=1, le=100),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_supervisor_or_admin),
):
    inb = await db.get(InboundNumber, inbound_id)
    if not inb or inb.business_id != business_id:
        raise HTTPException(status_code=404, detail="Inbound number not found")

    calls_q = (
        select(Call)
        .where(Call.to_number == inb.phone_number)
        .order_by(Call.created_at.desc())
        .limit(limit)
    )
    calls = (await db.scalars(calls_q)).all()

    return [
        InboundCallSummary(
            id=c.id,
            call_sid=c.call_sid or "",
            from_number=c.from_number or "",
            to_number=c.to_number or "",
            status=c.status or "COMPLETED",
            duration_seconds=c.duration_seconds or 0,
            client_name=c.client_name or "",
            agent_name=c.agent_type or "Inbound Route",
            created_at=c.created_at,
        )
        for c in calls
    ]


@router.post("/simulate-call")
async def simulate_inbound_call(
    payload: SimulateInboundCallPayload,
    db: AsyncSession = Depends(get_db),
    business_id: str = Depends(get_current_business_id_optional),
):
    from app.api.routes.websockets import global_ws_manager
    from app.services.inbound_routing_service import classify_inbound_call

    # Resolve inbound DID if specified
    to_num = payload.to_number or "+1 800 555 0199"
    if payload.inbound_number_id:
        inb = await db.get(InboundNumber, payload.inbound_number_id)
        if inb:
            to_num = inb.phone_number

    caller_num = payload.caller_phone or "+91 98765 43210"
    caller_nm = payload.caller_name or "Incoming Customer"

    session, lead, target_campaign = await classify_inbound_call(
        caller_number=caller_num,
        dest_number=to_num,
        default_tenant_id=business_id or "default",
        caller_name=caller_nm,
    )

    # Broadcast incoming call event to all active WebSocket clients with complete classification payload
    is_waiting = session.routing_type != "LEAD_CALLBACK_HUMAN"
    event_payload = {
        "event": "INCOMING_CALL",
        "call_id": session.call_id,
        "session_id": session.session_id,
        "caller_name": session.lead_name or caller_nm,
        "caller_phone": session.inbound_phone,
        "from_number": session.inbound_phone,
        "to_number": session.to_number,
        "agent_type": "VOICE",
        "agent_id": session.selected_agent_id,
        "target_user_id": session.selected_agent_id,
        "inbound_type": session.routing_type,
        "routing_type": session.routing_type,
        "routing_status": session.routing_status,
        "lead_id": session.lead_id,
        "lead_name": session.lead_name,
        "campaign_id": session.campaign_id,
        "campaign_name": session.campaign_name,
        "outbound_call_id": session.outbound_call_id,
        "outbound_call_time": session.outbound_call_time,
        "outbound_agent_id": session.outbound_agent_id,
        "outbound_agent_name": session.outbound_agent_name,
        "outbound_agent_type": session.outbound_agent_type,
        "selected_agent_id": session.selected_agent_id,
        "selected_agent_name": session.selected_agent_name,
        "waiting_for_agent": is_waiting,
        "status": session.call_status,
        "notes": payload.notes or session.call_result,
        "initiated_at": session.initiated_at,
        "created_at": session.created_at,
    }
    await global_ws_manager.broadcast(json.dumps(event_payload))

    return {
        "success": True,
        "message": f"Inbound call ({session.routing_type}) ringing from {session.lead_name}",
        "call_id": session.call_id,
        "routing_type": session.routing_type,
        "call": {
            "id": session.call_id,
            "client_name": session.lead_name,
            "client_phone": session.inbound_phone,
            "to_number": session.to_number,
            "routing_type": session.routing_type,
            "status": session.call_status,
            "initiated_at": session.initiated_at,
        }
    }



@router.post("/calls/{call_id}/attend")
async def attend_inbound_call(
    call_id: str,
    db: AsyncSession = Depends(get_db),
    business_id: str = Depends(get_current_business_id_optional),
):
    from app.api.routes.websockets import global_ws_manager

    call = await db.get(Call, call_id)
    if not call:
        call_stmt = select(Call).where(Call.id == call_id)
        call = (await db.scalars(call_stmt)).first()

    if not call:
        raise HTTPException(status_code=404, detail="Inbound call not found")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call.status = "IN_PROGRESS"
    call.connected_at = now
    call.client_connected_at = now
    call.call_result = "ANSWERED"
    call.updated_at = now
    db.add(call)
    await db.commit()
    await db.refresh(call)

    # If this was a live incoming SIP PBX call, answer on the SIP socket
    try:
        from app.services.sip_inbound_daemon import answer_incoming_sip_call
        await answer_incoming_sip_call(call.id)
    except Exception:
        pass

    try:
        from app.api.routes.barge_stream import _get_or_create_session
        b_sess = await _get_or_create_session(call.id)
        if b_sess:
            b_sess["running"] = True
            b_sess["is_voice_agent_call"] = True
            b_sess["human_handling"] = True
            b_sess["agent_type"] = "HUMAN"
    except Exception:
        pass

    # Broadcast answered event
    await global_ws_manager.broadcast(json.dumps({
        "event": "INCOMING_CALL_ANSWERED",
        "call_id": call.id,
        "client_name": call.client_name,
        "client_phone": call.client_phone,
        "status": "IN_PROGRESS",
        "connected_at": now.isoformat(),
    }))

    return {
        "success": True,
        "message": "Inbound call attended successfully",
        "call": {
            "id": call.id,
            "client_name": call.client_name,
            "client_phone": call.client_phone,
            "to_number": call.to_number,
            "agent_type": call.agent_type,
            "status": call.status,
            "connected_at": now.isoformat(),
        }
    }


@router.post("/calls/{call_id}/decline")
async def decline_inbound_call(
    call_id: str,
    db: AsyncSession = Depends(get_db),
    business_id: str = Depends(get_current_business_id_optional),
):
    from app.api.routes.websockets import global_ws_manager

    call = await db.get(Call, call_id)
    if not call:
        call_stmt = select(Call).where(Call.id == call_id)
        call = (await db.scalars(call_stmt)).first()

    if not call:
        raise HTTPException(status_code=404, detail="Inbound call not found")

    # If this was a live incoming SIP PBX call, send 486 Busy to PBX
    try:
        from app.services.sip_inbound_daemon import decline_incoming_sip_call
        await decline_incoming_sip_call(call.id)
    except Exception:
        pass

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call.status = "DECLINED"
    call.ended_at = now
    call.call_result = "Declined by agent"
    call.updated_at = now
    db.add(call)
    await db.commit()

    # Broadcast decline event
    await global_ws_manager.broadcast(json.dumps({
        "event": "INCOMING_CALL_DECLINED",
        "call_id": call.id,
        "status": "DECLINED",
    }))

    return {
        "success": True,
        "message": "Inbound call declined"
    }
