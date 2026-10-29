from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
import uuid
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.routes.websockets import global_ws_manager
from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Business, Call, Campaign, Conversation, Expert, InboundNumber, Lead, User


class InboundRoutingType:
    LEAD_CALLBACK_HUMAN = "LEAD_CALLBACK_HUMAN"
    AI_AGENT_CALLBACK = "AI_AGENT_CALLBACK"
    NORMAL_INBOUND = "NORMAL_INBOUND"


class InboundRoutingStatus:
    INCOMING = "INCOMING"
    CLASSIFYING = "CLASSIFYING"
    WAITING_FOR_ROUTE = "WAITING_FOR_ROUTE"
    ROUTE_REQUESTED = "ROUTE_REQUESTED"
    RINGING_AGENT = "RINGING_AGENT"
    TRANSFER_IN_PROGRESS = "TRANSFER_IN_PROGRESS"
    ROUTING_TO_ORIGINAL_AGENT = "ROUTING_TO_ORIGINAL_AGENT"
    CONNECTED = "CONNECTED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    NO_ANSWER = "NO_ANSWER"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class InboundCallSession:
    call_id: str
    session_id: str
    organization_id: str
    inbound_phone: str
    to_number: str
    routing_type: str  # LEAD_CALLBACK_HUMAN, AI_AGENT_CALLBACK, NORMAL_INBOUND
    routing_status: str  # WAITING_FOR_ROUTE, RINGING_AGENT, etc.
    lead_id: Optional[str] = None
    lead_name: Optional[str] = None
    campaign_id: Optional[str] = None
    campaign_name: Optional[str] = None
    outbound_call_id: Optional[str] = None
    outbound_call_time: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    outbound_agent_name: Optional[str] = None
    outbound_agent_type: Optional[str] = None  # AI_AGENT vs HUMAN_VOICE_AGENT
    selected_agent_id: Optional[str] = None
    selected_agent_name: Optional[str] = None
    transfer_status: str = "NONE"  # NONE, IN_PROGRESS, CONNECTED, FAILED
    call_status: str = "RINGING"
    initiated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    ended_at: Optional[str] = None
    is_active: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "call_id": self.call_id,
            "session_id": self.session_id,
            "organization_id": self.organization_id,
            "inbound_phone": self.inbound_phone,
            "from_number": self.inbound_phone,
            "to_number": self.to_number,
            "routing_type": self.routing_type,
            "inbound_type": self.routing_type,
            "routing_status": self.routing_status,
            "lead_id": self.lead_id,
            "lead_name": self.lead_name or "Not Found",
            "caller_name": self.lead_name or "Incoming Customer",
            "caller_phone": self.inbound_phone,
            "campaign_id": self.campaign_id,
            "campaign_name": self.campaign_name or "N/A",
            "outbound_call_id": self.outbound_call_id,
            "outbound_call_time": self.outbound_call_time,
            "outbound_agent_id": self.outbound_agent_id,
            "outbound_agent_name": self.outbound_agent_name or "N/A",
            "outbound_agent_type": self.outbound_agent_type or "N/A",
            "selected_agent_id": self.selected_agent_id,
            "selected_agent_name": self.selected_agent_name,
            "target_user_id": self.selected_agent_id,
            "transfer_status": self.transfer_status,
            "call_status": self.call_status,
            "status": self.routing_status,
            "waiting_for_agent": self.routing_status in (InboundRoutingStatus.WAITING_FOR_ROUTE, InboundRoutingStatus.ROUTE_REQUESTED),
            "initiated_at": self.initiated_at,
            "created_at": self.created_at,
            "ended_at": self.ended_at,
            "is_active": self.is_active,
        }


class InboundSessionRegistry:
    """Thread-safe multi-tenant registry for in-memory active inbound call sessions."""

    def __init__(self):
        self._sessions: Dict[str, InboundCallSession] = {}
        self._lock = asyncio.Lock()

    async def register(self, session: InboundCallSession) -> InboundCallSession:
        async with self._lock:
            self._sessions[session.call_id] = session
            return session

    async def get(self, call_id: str) -> Optional[InboundCallSession]:
        async with self._lock:
            return self._sessions.get(call_id)

    async def update_status(self, call_id: str, status: str, transfer_status: Optional[str] = None) -> Optional[InboundCallSession]:
        async with self._lock:
            sess = self._sessions.get(call_id)
            if sess:
                sess.routing_status = status
                sess.status = status
                if transfer_status:
                    sess.transfer_status = transfer_status
                if status in (InboundRoutingStatus.COMPLETED, InboundRoutingStatus.CANCELLED, InboundRoutingStatus.FAILED):
                    sess.is_active = False
                    sess.ended_at = datetime.now(timezone.utc).isoformat()
            return sess

    async def assign_agent(self, call_id: str, agent_id: str, agent_name: str) -> Optional[InboundCallSession]:
        async with self._lock:
            sess = self._sessions.get(call_id)
            if sess:
                sess.selected_agent_id = agent_id
                sess.selected_agent_name = agent_name
                sess.routing_status = InboundRoutingStatus.RINGING_AGENT
                sess.transfer_status = "IN_PROGRESS"
                sess.call_status = "RINGING"
            return sess

    async def close(self, call_id: str, reason: str = "COMPLETED") -> Optional[InboundCallSession]:
        async with self._lock:
            sess = self._sessions.get(call_id)
            if sess:
                sess.is_active = False
                sess.routing_status = InboundRoutingStatus.COMPLETED if reason == "COMPLETED" else InboundRoutingStatus.CANCELLED
                sess.ended_at = datetime.now(timezone.utc).isoformat()
            return sess

    async def list_active(self, organization_id: Optional[str] = None) -> List[InboundCallSession]:
        async with self._lock:
            results = []
            now_dt = datetime.now(timezone.utc)
            for s in list(self._sessions.values()):
                if s.created_at:
                    try:
                        c_dt = datetime.fromisoformat(s.created_at)
                        if c_dt.tzinfo is None:
                            c_dt = c_dt.replace(tzinfo=timezone.utc)
                        if (now_dt - c_dt).total_seconds() > 45.0:
                            s.is_active = False
                            s.routing_status = InboundRoutingStatus.CANCELLED
                            s.call_status = "MISSED"
                    except Exception:
                        pass
                if s.is_active:
                    if not organization_id or s.organization_id == organization_id:
                        results.append(s)
            return sorted(results, key=lambda x: x.created_at, reverse=True)


inbound_session_registry = InboundSessionRegistry()


def normalize_phone_number(raw_phone: str) -> Tuple[str, str]:
    """Standardizes phone number for exact/suffix matching."""
    cleaned = re.sub(r"[^\d+]", "", raw_phone.strip())
    digits_only = re.sub(r"\D", "", cleaned)
    last_10 = digits_only[-10:] if len(digits_only) >= 10 else digits_only
    return cleaned, last_10


async def classify_inbound_call(
    caller_number: str,
    dest_number: str,
    default_tenant_id: str,
    caller_name: str = "Caller",
    call_id_val: str = "",
) -> Tuple[InboundCallSession, Optional[Lead], Optional[Campaign]]:
    """
    CENTRALIZED AUTHORITATIVE INBOUND ROUTING DECISION:
    
    1. Normalize incoming number
    2. Find matching Lead in organization
    3. If Lead exists, query latest relevant outbound call (same Lead + same Org + outbound direction)
    4. Determine outbound agent type:
       - HUMAN_VOICE_AGENT -> TYPE 1 (LEAD_CALLBACK_HUMAN) -> Direct route to original Human Agent
       - AI_AGENT          -> TYPE 3 (AI_AGENT_CALLBACK)  -> Admin UI -> Admin routes to available Human Agent
       - ELSE              -> TYPE 2 (NORMAL_INBOUND)     -> Admin UI -> Admin routes to available Human Agent
    """
    norm_phone, last_10 = normalize_phone_number(caller_number)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    now_iso = now.isoformat()
    db_call_id = str(uuid.uuid4()) if not call_id_val else call_id_val

    logger.info(f"[INBOUND_CALL] caller_phone={caller_number} dest_phone={dest_number} raw_call_id={db_call_id}")
    logger.info(f"[INBOUND_PHONE_NORMALIZED] norm={norm_phone} last_10={last_10}")

    target_tenant = default_tenant_id
    lead: Optional[Lead] = None
    target_campaign: Optional[Campaign] = None
    routing_type = InboundRoutingType.NORMAL_INBOUND
    routing_status = InboundRoutingStatus.WAITING_FOR_ROUTE
    outbound_call_id: Optional[str] = None
    outbound_call_time: Optional[str] = None
    outbound_agent_id: Optional[str] = None
    outbound_agent_name: Optional[str] = None
    outbound_agent_type: Optional[str] = None
    target_human_user_id: Optional[str] = None
    target_human_name: Optional[str] = None

    async with AsyncSessionLocal() as db:
        # Resolve tenant from destination number via SIPAccount or InboundNumber
        if dest_number:
            from app.db.models import SIPAccount
            clean_dest = re.sub(r"\D", "", dest_number)
            sip_acc_stmt = select(SIPAccount).where(
                or_(
                    SIPAccount.caller_id == dest_number,
                    SIPAccount.caller_id == f"+{clean_dest}",
                    SIPAccount.username == dest_number,
                )
            )
            matched_sip_acc = (await db.scalars(sip_acc_stmt)).first()
            if matched_sip_acc and matched_sip_acc.organization_id:
                target_tenant = matched_sip_acc.organization_id
                logger.info(f"[INBOUND_SIP_MATCH] dest_number={dest_number} matched SIPAccount '{matched_sip_acc.name}' for tenant={target_tenant}")

        # Resolve tenant from active admin user if default tenant is fallback
        if not target_tenant or target_tenant == "primary-business":
            user_stmt = select(User).where(User.is_active == True)
            u_row = (await db.scalars(user_stmt)).first()
            if u_row and u_row.business_id:
                target_tenant = u_row.business_id

        # 1. Lead Matching (Organization Scoped)
        lead_stmt = select(Lead).where(
            or_(
                Lead.phone == caller_number,
                Lead.phone == f"+{caller_number}",
                Lead.phone == norm_phone,
                Lead.phone.like(f"%{last_10}%") if len(last_10) >= 7 else False,
            )
        )
        if target_tenant:
            lead_stmt = lead_stmt.where(
                or_(
                    Lead.business_id == target_tenant,
                    Lead.business_id == None,
                    Lead.business_id == "default",
                    Lead.business_id == "primary-business",
                )
            )
        
        lead = (await db.scalars(lead_stmt)).first()

        if lead:
            logger.info(f"[LEAD_MATCH] lead_id={lead.id} lead_name='{lead.first_name} {lead.last_name}' business_id={lead.business_id}")
            target_tenant = lead.business_id or target_tenant

            # 2. Latest Relevant Outbound Call Query (Same Lead + Same Org + Outbound Direction)
            outbound_stmt = (
                select(Call)
                .where(
                    Call.lead_id == lead.id,
                    Call.direction == "OUTBOUND",
                    or_(
                        Call.business_id == target_tenant,
                        Call.business_id == None,
                        Call.business_id == "default",
                        Call.business_id == "primary-business",
                    ),
                )
                .order_by(Call.initiated_at.desc(), Call.created_at.desc())
            )
            matched_outbound = (await db.scalars(outbound_stmt)).first()

            if matched_outbound:
                outbound_call_id = matched_outbound.id
                outbound_call_time = (matched_outbound.initiated_at or matched_outbound.created_at or now).isoformat()
                logger.info(f"[OUTBOUND_CALL_MATCH] outbound_call_id={matched_outbound.id} agent_id={matched_outbound.agent_id} user_id={matched_outbound.user_id} agent_type={matched_outbound.agent_type}")

                # Campaign resolution
                if matched_outbound.campaign_id:
                    target_campaign = await db.get(Campaign, matched_outbound.campaign_id)
                    logger.info(f"[CAMPAIGN_RESOLUTION] campaign_id={target_campaign.id} name='{target_campaign.name}'")

                # 3. Determine Outbound Agent Type
                # Check if outbound was AI Agent
                is_ai_outbound = False
                raw_agent_type = (matched_outbound.agent_type or "").upper()

                if raw_agent_type in ("AI", "AI_VOICE", "AI_AGENT", "VOICE_AGENT_AI"):
                    is_ai_outbound = True
                elif matched_outbound.agent_id:
                    ai_row = await db.get(AIAgent, matched_outbound.agent_id)
                    if ai_row:
                        is_ai_outbound = True
                        outbound_agent_id = ai_row.id
                        outbound_agent_name = ai_row.name

                if is_ai_outbound:
                    # TYPE 2: AI AGENT CALLBACK -> Admin Routing
                    routing_type = InboundRoutingType.AI_AGENT_CALLBACK
                    routing_status = InboundRoutingStatus.WAITING_FOR_ROUTE
                    outbound_agent_type = "AI_AGENT"
                    if not outbound_agent_name:
                        outbound_agent_name = "AI Voice Agent"
                    logger.info(
                        f"[AI_AGENT_CALLBACK_DETECTED] call_id={db_call_id} lead_id={lead.id} "
                        f"outbound_call_id={matched_outbound.id} original_ai_agent='{outbound_agent_name}'"
                    )
                else:
                    # Check for Human Voice Agent
                    orig_human: Optional[User | Expert] = None
                    if matched_outbound.user_id:
                        orig_human = await db.get(User, matched_outbound.user_id)
                    if not orig_human and matched_outbound.agent_id:
                        orig_human = await db.get(User, matched_outbound.agent_id)
                        if not orig_human:
                            exp = await db.get(Expert, matched_outbound.agent_id)
                            if exp and exp.user_id:
                                orig_human = await db.get(User, exp.user_id)
                            elif exp:
                                orig_human = exp

                    if not orig_human and target_campaign:
                        if target_campaign.voice_agent_id:
                            exp = await db.get(Expert, target_campaign.voice_agent_id)
                            if exp and exp.user_id:
                                orig_human = await db.get(User, exp.user_id)
                            elif exp:
                                orig_human = exp
                        elif target_campaign.user_id:
                            orig_human = await db.get(User, target_campaign.user_id)

                    # Validate Human Agent Organization Ownership
                    if orig_human and (not getattr(orig_human, "business_id", None) or orig_human.business_id in (target_tenant, "default", "primary-business", None)):
                        # TYPE 1: HUMAN VOICE AGENT CALLBACK -> Direct Route to Original Human Agent
                        routing_type = InboundRoutingType.LEAD_CALLBACK_HUMAN
                        routing_status = InboundRoutingStatus.ROUTING_TO_ORIGINAL_AGENT
                        outbound_agent_type = "HUMAN_VOICE_AGENT"
                        target_human_user_id = orig_human.id
                        target_human_name = getattr(orig_human, "full_name", None) or getattr(orig_human, "name", None) or getattr(orig_human, "email", "Voice Agent")
                        outbound_agent_id = orig_human.id
                        outbound_agent_name = target_human_name
                        logger.info(
                            f"[ORIGINAL_HUMAN_AGENT_RESOLUTION] call_id={db_call_id} lead_id={lead.id} "
                            f"target_human_agent='{target_human_name}' ({target_human_user_id})"
                        )
                    else:
                        # Fallback to Normal Inbound Admin Routing
                        routing_type = InboundRoutingType.NORMAL_INBOUND
                        routing_status = InboundRoutingStatus.WAITING_FOR_ROUTE
                        outbound_agent_type = "UNKNOWN"
                        logger.info(f"[NORMAL_INBOUND_DETECTED] call_id={db_call_id} reason='Human agent not resolved for outbound call'")
            else:
                # Lead exists but no outbound call -> Check if assigned_agent_id is set on Lead
                lead_assigned_user = None
                if getattr(lead, "assigned_agent_id", None):
                    lead_assigned_user = await db.get(User, lead.assigned_agent_id)

                if lead_assigned_user:
                    routing_type = InboundRoutingType.LEAD_CALLBACK_HUMAN
                    routing_status = InboundRoutingStatus.ROUTING_TO_ORIGINAL_AGENT
                    outbound_agent_type = "HUMAN_VOICE_AGENT"
                    target_human_user_id = lead_assigned_user.id
                    target_human_name = getattr(lead_assigned_user, "full_name", None) or getattr(lead_assigned_user, "email", "Voice Agent")
                    logger.info(f"[ORIGINAL_HUMAN_AGENT_RESOLUTION] call_id={db_call_id} lead_id={lead.id} target_human_agent='{target_human_name}' via lead.assigned_agent_id")
                else:
                    routing_type = InboundRoutingType.NORMAL_INBOUND
                    routing_status = InboundRoutingStatus.WAITING_FOR_ROUTE
                    logger.info(f"[NORMAL_INBOUND_DETECTED] call_id={db_call_id} reason='No outbound call history or assigned agent for lead'")

        else:
            # Lead not found -> Create new Lead record & set as Normal Inbound
            routing_type = InboundRoutingType.NORMAL_INBOUND
            routing_status = InboundRoutingStatus.WAITING_FOR_ROUTE
            lead = Lead(
                business_id=target_tenant,
                phone=caller_number,
                first_name=caller_name if caller_name and caller_name != "Unknown" else "Caller",
                source="INBOUND_SIP",
                status="NEW",
            )
            db.add(lead)
            await db.flush()
            logger.info(f"[NORMAL_INBOUND_DETECTED] call_id={db_call_id} reason='Unknown caller, auto-created lead {lead.id}'")

        # Create / link Conversation record
        conversation = Conversation(
            business_id=target_tenant,
            lead_id=lead.id if lead else None,
            agent_id=target_human_user_id,
            channel="VOICE",
            status="ACTIVE",
        )
        db.add(conversation)
        await db.flush()

        # Create Inbound Call Record in DB
        effective_status = "RINGING" if routing_type == InboundRoutingType.LEAD_CALLBACK_HUMAN else "WAITING_FOR_ROUTE"
        effective_result = (
            f"Routing to original human agent {target_human_name}"
            if routing_type == InboundRoutingType.LEAD_CALLBACK_HUMAN
            else ("AI Agent callback waiting for route" if routing_type == InboundRoutingType.AI_AGENT_CALLBACK else "Normal inbound waiting for route")
        )

        db_call = Call(
            id=db_call_id,
            business_id=target_tenant,
            user_id=target_human_user_id,
            agent_id=target_human_user_id,
            campaign_id=target_campaign.id if target_campaign else None,
            conversation_id=conversation.id,
            lead_id=lead.id if lead else None,
            direction="INBOUND",
            status=effective_status,
            from_number=caller_number,
            to_number=dest_number,
            client_name=(f"{lead.first_name} {lead.last_name}".strip() if lead else caller_name) or caller_name,
            client_phone=caller_number,
            sip_extension=dest_number,
            agent_type="VOICE",
            call_result=effective_result,
            call_sid=f"sip-in-{db_call_id[:12]}",
            initiated_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(db_call)
        await db.commit()

    # Create InboundCallSession in memory
    session = InboundCallSession(
        call_id=db_call_id,
        session_id=f"sip-in-{db_call_id[:12]}",
        organization_id=target_tenant,
        inbound_phone=caller_number,
        to_number=dest_number,
        routing_type=routing_type,
        routing_status=routing_status,
        lead_id=lead.id if lead else None,
        lead_name=f"{lead.first_name} {lead.last_name}".strip() if lead else caller_name,
        campaign_id=target_campaign.id if target_campaign else None,
        campaign_name=target_campaign.name if target_campaign else None,
        outbound_call_id=outbound_call_id,
        outbound_call_time=outbound_call_time,
        outbound_agent_id=outbound_agent_id,
        outbound_agent_name=outbound_agent_name,
        outbound_agent_type=outbound_agent_type,
        selected_agent_id=target_human_user_id,
        selected_agent_name=target_human_name,
        call_status=effective_status,
        initiated_at=now_iso,
        created_at=now_iso,
    )
    await inbound_session_registry.register(session)

    logger.info(
        f"[INBOUND ROUTING]\n"
        f"call_id={db_call_id}\n"
        f"session_id={session.session_id}\n"
        f"organization_id={target_tenant}\n"
        f"lead_id={session.lead_id}\n"
        f"outbound_call_id={outbound_call_id}\n"
        f"outbound_agent_type={outbound_agent_type}\n"
        f"routing_type={routing_type}\n"
        f"routing_status={routing_status}"
    )

    return session, lead, target_campaign


async def get_organization_agent_availability(db: AsyncSession, business_id: Optional[str]) -> List[Dict[str, Any]]:
    """
    Authoritative real-time agent availability checker:
    States: AVAILABLE, RINGING, IN_CALL, BUSY, OFFLINE.
    Only AVAILABLE agents have is_available=True.
    """
    user_stmt = select(User).where(
        User.is_active == True,
        User.role.in_(["VOICE_AGENT", "AGENT", "SUPERVISOR", "ADMIN", "SALES_EXPERT", "AGENT_MANAGER"])
    )
    if business_id:
        user_stmt = user_stmt.where(or_(User.business_id == business_id, User.business_id == None))
    users = (await db.scalars(user_stmt)).all()

    exp_stmt = select(Expert)
    if business_id:
        exp_stmt = exp_stmt.where(or_(Expert.business_id == business_id, Expert.business_id == None))
    experts = (await db.scalars(exp_stmt)).all()

    # Query active calls
    active_calls_stmt = select(Call).where(
        Call.status.in_(["RINGING", "RINGING_AGENT", "IN_PROGRESS", "CONNECTED", "HUMAN_HANDLING", "TALKING", "TRANSFER_IN_PROGRESS"]),
        Call.ended_at.is_(None),
    )
    if business_id:
        active_calls_stmt = active_calls_stmt.where(Call.business_id == business_id)
    active_calls = (await db.scalars(active_calls_stmt)).all()

    ringing_agent_ids = set()
    in_call_agent_ids = set()

    for c in active_calls:
        target_id = c.user_id or c.agent_id
        if not target_id:
            continue
        if c.status in ("RINGING", "RINGING_AGENT", "TRANSFER_IN_PROGRESS"):
            ringing_agent_ids.add(target_id)
        else:
            in_call_agent_ids.add(target_id)

    seen_ids = set()
    results = []

    for u in users:
        if u.id in seen_ids:
            continue
        seen_ids.add(u.id)

        if not u.is_active:
            status = "OFFLINE"
        elif u.id in in_call_agent_ids:
            status = "IN_CALL"
        elif u.id in ringing_agent_ids:
            status = "RINGING"
        elif getattr(u, "status", "AVAILABLE").upper() in ("BUSY", "PAUSED"):
            status = "BUSY"
        elif getattr(u, "status", "AVAILABLE").upper() == "OFFLINE":
            status = "OFFLINE"
        else:
            status = "AVAILABLE"

        agent_name = u.full_name or u.email.split("@")[0]
        results.append({
            "id": u.id,
            "name": agent_name,
            "type": "HUMAN_VOICE_AGENT",
            "role": u.role,
            "email": u.email,
            "status": status,
            "is_available": (status == "AVAILABLE"),
        })

    for exp in experts:
        target_id = exp.user_id or exp.id
        if target_id in seen_ids:
            continue
        seen_ids.add(target_id)

        if target_id in in_call_agent_ids:
            status = "IN_CALL"
        elif target_id in ringing_agent_ids:
            status = "RINGING"
        elif exp.status in ("BUSY", "PAUSED"):
            status = "BUSY"
        elif exp.status in ("OFFLINE", "INACTIVE"):
            status = "OFFLINE"
        else:
            status = "AVAILABLE"

        results.append({
            "id": target_id,
            "name": exp.name,
            "type": "HUMAN_VOICE_AGENT",
            "role": "VOICE_AGENT",
            "email": exp.email,
            "status": status,
            "is_available": (status == "AVAILABLE"),
        })

    return results


async def execute_live_route_transfer(
    call_id: str,
    target_agent_id: str,
    db: AsyncSession,
    business_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Executes Admin Live Transfer with double-route protection and authoritative availability validation.
    """
    logger.info(f"[ROUTE_REQUEST] call_id={call_id} target_agent_id={target_agent_id} business_id={business_id}")

    # 1. Validate Inbound Session
    session = await inbound_session_registry.get(call_id)
    call = await db.get(Call, call_id)
    if not call:
        call_stmt = select(Call).where(Call.id == call_id)
        call = (await db.scalars(call_stmt)).first()

    if not call:
        logger.warning(f"[ROUTE_REJECTED] reason='Call {call_id} not found in database'")
        raise ValueError(f"Inbound call {call_id} was not found")

    # 2. Organization Isolation Check
    def _is_matching_biz(b1: Optional[str], b2: Optional[str]) -> bool:
        if not b1 or not b2:
            return True
        if b1 in ("default", "primary-business") or b2 in ("default", "primary-business"):
            return True
        return b1 == b2

    if not _is_matching_biz(call.business_id, business_id):
        logger.warning(f"[ROUTE_REJECTED] reason='Organization mismatch: call.business_id={call.business_id} != request={business_id}'")
        raise PermissionError("Call does not belong to the active organization")

    # 3. Double-Route Protection: Ensure call is still WAITING_FOR_ROUTE
    if call.status not in ("WAITING_FOR_ROUTE", "RINGING") or (call.user_id and call.status == "IN_PROGRESS"):
        logger.warning(f"[ROUTE_REJECTED] reason='Call {call_id} is already in state {call.status} (double-route prevented)'")
        raise RuntimeError("CALL_ALREADY_ROUTED")

    # 4. Check Customer Call is still active (not ended/cancelled)
    if call.ended_at or call.status in ("COMPLETED", "CANCELLED", "FAILED", "DECLINED"):
        logger.warning(f"[ROUTE_REJECTED] reason='Call {call_id} is no longer active (customer hung up)'")
        raise RuntimeError("CALL_NOT_ACTIVE")

    # 5. Resolve Target Human Voice Agent
    target_user = await db.get(User, target_agent_id)
    target_expert = None
    if not target_user:
        target_expert = await db.get(Expert, target_agent_id)
        if target_expert and target_expert.user_id:
            target_user = await db.get(User, target_expert.user_id)

    if not target_user and not target_expert:
        logger.warning(f"[ROUTE_REJECTED] reason='Target human agent {target_agent_id} not found'")
        raise ValueError("Selected Human Voice Agent does not exist")

    # Verify agent belongs to same organization
    agent_biz_id = getattr(target_user, "business_id", None) or getattr(target_expert, "business_id", None)
    if not _is_matching_biz(agent_biz_id, business_id):
        logger.warning(f"[ROUTE_REJECTED] reason='Target agent organization mismatch'")
        raise PermissionError("Selected agent belongs to another organization")

    agent_name = (
        (target_user.full_name or target_user.email)
        if target_user
        else (target_expert.name if target_expert else "Voice Agent")
    )

    # 6. Revalidate Agent Availability in real time
    active_agent_call_stmt = select(Call).where(
        or_(Call.user_id == target_agent_id, Call.agent_id == target_agent_id),
        Call.id != call_id,
        Call.status.in_(["RINGING", "RINGING_AGENT", "IN_PROGRESS", "CONNECTED", "HUMAN_HANDLING", "TALKING", "TRANSFER_IN_PROGRESS"]),
        Call.ended_at.is_(None),
    )
    existing_call = (await db.scalars(active_agent_call_stmt)).first()
    if existing_call:
        logger.warning(f"[ROUTE_REJECTED] reason='Human agent {agent_name} is currently busy on call {existing_call.id}'")
        raise RuntimeError(f"Human Voice Agent {agent_name} is no longer available (currently busy on another call)")

    # 7. Apply Atomic State Transition
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    call.user_id = target_agent_id
    call.agent_id = target_agent_id
    call.agent_type = "VOICE"
    call.status = "RINGING"
    call.call_result = f"Live transferred to Human Agent {agent_name}"
    call.updated_at = now
    db.add(call)
    await db.commit()
    await db.refresh(call)

    # Update in-memory session
    await inbound_session_registry.assign_agent(call_id, target_agent_id, agent_name)

    # Update active SIP call transaction in daemon
    from app.services.sip_inbound_daemon import ACTIVE_SIP_CALLS
    tx = ACTIVE_SIP_CALLS.get(call_id)
    if tx:
        tx["user_id"] = target_agent_id
        tx["agent_id"] = target_agent_id
        tx["is_ai_agent"] = False
        tx["status"] = "RINGING"

    logger.info(f"[ROUTE_ACCEPTED] call_id={call_id} target_agent='{agent_name}' ({target_agent_id})")
    logger.info(f"[LIVE_TRANSFER] status=TRANSFER_STARTED call_id={call_id} destination_agent={target_agent_id}")

    # 8. Real-Time Broadcasts
    # A) Broadcast to all Admin dashboards that call has been routed
    await global_ws_manager.broadcast(json.dumps({
        "event": "INCOMING_CALL_ROUTED",
        "call_id": call.id,
        "selected_agent_id": target_agent_id,
        "selected_agent_name": agent_name,
        "routing_status": InboundRoutingStatus.RINGING_AGENT,
        "status": "RINGING",
        "transfer_status": "IN_PROGRESS",
    }))

    # B) Broadcast targeted ring event to the selected Human Voice Agent's browser softphone
    await global_ws_manager.broadcast(json.dumps({
        "event": "INCOMING_CALL",
        "call_id": call.id,
        "caller_name": call.client_name or "Incoming Customer",
        "caller_phone": call.client_phone or call.from_number,
        "from_number": call.from_number,
        "to_number": call.to_number,
        "agent_type": "VOICE",
        "agent_id": target_agent_id,
        "target_user_id": target_agent_id,
        "inbound_type": session.routing_type if session else "ADMIN_ROUTED",
        "routing_type": session.routing_type if session else "ADMIN_ROUTED",
        "waiting_for_agent": False,
        "status": "RINGING",
        "notes": f"Live incoming call transferred to {agent_name}",
        "initiated_at": now.isoformat(),
        "created_at": now.isoformat(),
    }))

    return {
        "success": True,
        "message": f"Inbound call successfully routed to Human Voice Agent {agent_name}",
        "call_id": call.id,
        "agent_id": target_agent_id,
        "agent_name": agent_name,
        "routing_status": InboundRoutingStatus.RINGING_AGENT,
    }
