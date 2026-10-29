import uuid
import traceback
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, delete, func, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_business_id, get_current_db_user
from app.core.security import hash_password
from app.db.database import get_db
from app.db.models import Expert, Lead, Campaign, CampaignLead, User, LeadAssignment, Business, Deal, Appointment, DialerRun, Call
from app.schemas.expert import ExpertOut
from app.schemas.lead import LeadOut
from app.schemas.campaign import CampaignOut

router = APIRouter(prefix="/api/voice-agents", tags=["Voice Agents"])


@router.get("", response_model=List[ExpertOut])
@router.get("/", response_model=List[ExpertOut])
async def list_voice_agents(
    agent_type: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    biz_id = user.business_id or ""
    try:
        if not biz_id and (user.role or "").upper() != "SUPER_ADMIN":
            return []

        va_stmt = select(User).where(func.upper(User.role).in_(["VOICE_AGENT", "AGENT"]))
        if biz_id:
            va_stmt = va_stmt.where(User.business_id == biz_id)
        va_users_res = await db.scalars(va_stmt)
        for va_user in va_users_res.all():
            exp_check = (await db.scalars(select(Expert).where((Expert.user_id == va_user.id) | (Expert.email == va_user.email)))).first()
            if not exp_check:
                exp_biz = va_user.business_id or biz_id
                exp = Expert(
                    id=str(uuid.uuid4()),
                    business_id=exp_biz,
                    user_id=va_user.id,
                    name=va_user.full_name or va_user.email,
                    email=va_user.email,
                    skills=["INBOUND"],
                    status="AVAILABLE",
                    active_lead_count=0,
                )
                db.add(exp)
            else:
                if not exp_check.user_id:
                    exp_check.user_id = va_user.id
                if not exp_check.business_id and va_user.business_id:
                    exp_check.business_id = va_user.business_id

        await db.commit()
    except Exception as exc:
        logger.warning(f"Note on auto-seeding experts: {exc}")
        await db.rollback()

    try:
        exp_query = select(Expert).order_by(Expert.created_at.desc())
        if biz_id:
            exp_query = exp_query.where(Expert.business_id == biz_id)
        elif (user.role or "").upper() != "SUPER_ADMIN":
            return []
        result = await db.scalars(exp_query)
        experts = list(result.all())
        filtered_experts = []

        for e in experts:
            if e.skills is None:
                e.skills = []
            if not e.business_id:
                e.business_id = biz_id
            e.auto_dial_enabled = bool(getattr(e, "auto_dial_enabled", False))
            
            # Determine agent_type and exclude any non-voice-agent roles (Admins, Supervisors, etc.)
            skills_upper = [str(s).upper() for s in (e.skills or [])]
            u = None
            if e.user_id:
                u = await db.get(User, e.user_id)
            if not u and e.email:
                u = (await db.scalars(select(User).where(User.email == e.email))).first()

            # Strictly ensure only voice agents are listed - never show admins, supervisors, or owners
            if u and u.role and u.role.upper() not in ["VOICE_AGENT", "AGENT", "INBOUND_AGENT", "OUTBOUND_AGENT"]:
                continue
            if e.email and any(adm in e.email.lower() for adm in ["admin@callzenza.com", "admin@callmira.com", "superadmin@", "superv@gmail.com"]):
                continue

            computed_type = "INBOUND"
            if u and u.role in ["SUPER_ADMIN", "ADMIN"]:
                continue
            elif "OUTBOUND" in skills_upper or (u and u.permissions and u.permissions.get("agent_type") == "OUTBOUND") or (u and u.role == "OUTBOUND_AGENT"):
                computed_type = "OUTBOUND"
            elif "INBOUND" in skills_upper or (u and u.permissions and u.permissions.get("agent_type") == "INBOUND") or (u and u.role == "INBOUND_AGENT"):
                computed_type = "INBOUND"
            elif "ADMIN" in skills_upper:
                continue
            else:
                computed_type = "INBOUND"

            e.agent_type = computed_type
            e.direction = computed_type

            # Auto-compute active call count, calls handled, and campaign-linked leads
            camps_res = await db.execute(
                select(Campaign).where(
                    or_(
                        Campaign.voice_agent_id == e.id,
                        Campaign.voice_agent_id == e.user_id,
                        and_(Campaign.voice_agent_id.isnot(None), Campaign.voice_agent_id.like(f"%{e.id}%"))
                    )
                )
            )
            assigned_camps = camps_res.scalars().all()
            camp_ids = [c.id for c in assigned_camps]

            call_conds = [Call.agent_id == e.id]
            if e.user_id:
                call_conds.append(Call.agent_id == e.user_id)
            if camp_ids:
                call_conds.append(Call.campaign_id.in_(camp_ids))

            ACTIVE_CALL_STATUSES = [
                "IN_PROGRESS", "RINGING", "CLIENT_ADDING", "CONFERENCE_3WAY_ACTIVE",
                "HUMAN_HANDLING", "TRANSFER_REQUESTED", "DIALING", "CONNECTED", "TALKING", "ON_CALL",
                "CALLING", "IN_CALL", "RESERVED", "AI_HANDLING", "QUEUED", "ACTIVE", "LIVE", "BRIDGED",
                "initiating", "ringing", "answered", "in_progress"
            ]
            HANDLED_CALL_STATUSES = [
                "COMPLETED", "QUALIFIED", "NOT_QUALIFIED", "HUMAN_HANDLING",
                "CALLBACK_REQUIRED", "ANSWERED", "CLOSED", "TRANSFERRED", "SALE_SUCCESS", "IN_PROGRESS"
            ]

            stmt_active = select(func.count(Call.id)).where(
                and_(
                    or_(*call_conds),
                    Call.status.in_(ACTIVE_CALL_STATUSES)
                )
            )
            live_active_cnt = (await db.execute(stmt_active)).scalar() or 0

            stmt_handled = select(func.count(Call.id)).where(
                and_(
                    or_(*call_conds),
                    or_(
                        Call.status.in_(HANDLED_CALL_STATUSES),
                        (Call.duration_seconds.isnot(None) & (Call.duration_seconds > 0))
                    )
                )
            )
            handled_cnt = (await db.execute(stmt_handled)).scalar() or 0

            # Leads count
            direct_leads_stmt = select(func.count(Lead.id)).where(
                or_(Lead.assigned_expert_id == e.id, Lead.assigned_expert_id == e.user_id) if e.user_id else (Lead.assigned_expert_id == e.id)
            )
            direct_leads_cnt = (await db.execute(direct_leads_stmt)).scalar() or 0
            camp_leads_cnt = 0
            if camp_ids:
                camp_leads_cnt = (await db.execute(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id.in_(camp_ids)))).scalar() or 0
            total_leads_cnt = direct_leads_cnt + camp_leads_cnt

            e.active_calls = live_active_cnt
            e.calls_handled = handled_cnt
            e.total_leads = total_leads_cnt
            e.active_lead_count = live_active_cnt if live_active_cnt > 0 else handled_cnt
            if live_active_cnt > 0:
                e.status = "BUSY"

            # Apply filtering if agent_type requested
            if agent_type:
                target_t = agent_type.strip().upper()
                if target_t == "INBOUND" and computed_type != "INBOUND":
                    continue
                elif target_t == "OUTBOUND" and computed_type != "OUTBOUND":
                    continue
                elif target_t != "ALL" and computed_type != target_t:
                    continue

            filtered_experts.append(e)

        return filtered_experts
    except Exception as exc:
        logger.error(f"Error in list_voice_agents: {exc}\n{traceback.format_exc()}")
        return []


@router.post("", response_model=ExpertOut, status_code=201)
@router.post("/", response_model=ExpertOut, status_code=201)
async def create_voice_agent(
    body: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        business_id = user.business_id
        if not business_id:
            if (user.role or "").upper() == "SUPER_ADMIN":
                business_id = body.get("business_id")
            if not business_id:
                raise HTTPException(status_code=400, detail="Business ID is required to create a voice agent")

        name = body.get("name") or body.get("email") or "Voice Agent"
        email = body.get("email", "").strip().lower()
        phone = body.get("phone", "")
        status = body.get("status", "AVAILABLE")
        password = body.get("password")
        direction = (body.get("direction") or body.get("agent_type") or "INBOUND").strip().upper()

        # Prevent duplicate Expert record within this business
        if email:
            existing_expert = await db.scalar(
                select(Expert).where(Expert.email == email, Expert.business_id == business_id)
            )
            if existing_expert:
                if name and existing_expert.name != name:
                    existing_expert.name = name
                existing_expert.status = status
                if not existing_expert.business_id:
                    existing_expert.business_id = business_id
                if not existing_expert.skills:
                    existing_expert.skills = [direction]
                if getattr(existing_expert, "auto_dial_enabled", None) is None:
                    existing_expert.auto_dial_enabled = False
                await db.commit()
                await db.refresh(existing_expert)
                existing_expert.agent_type = direction
                existing_expert.direction = direction
                existing_expert.active_calls = 0
                existing_expert.calls_handled = 0
                existing_expert.total_leads = 0
                return existing_expert

        user_id = None
        if email:
            norm_email = email.strip().lower()
            user_res = await db.execute(select(User).where(User.email == norm_email))
            existing_user = user_res.scalars().first()
            if existing_user:
                existing_user.role = "VOICE_AGENT"
                if name and existing_user.full_name != name:
                    existing_user.full_name = name
                if password and password.strip():
                    existing_user.hashed_password = hash_password(password.strip())
                    existing_user.plain_password = password.strip()
                if not existing_user.business_id:
                    existing_user.business_id = business_id
                db.add(existing_user)
                user_id = existing_user.id
            else:
                raw_pwd = (password or "").strip() or "123456789"
                new_user = User(
                    id=str(uuid.uuid4()),
                    email=norm_email,
                    hashed_password=hash_password(raw_pwd),
                    plain_password=raw_pwd,
                    role="VOICE_AGENT",
                    full_name=name or norm_email.split("@")[0].capitalize(),
                    is_active=True,
                    email_verified=True,
                    business_id=business_id,
                )
                db.add(new_user)
                await db.flush()
                user_id = new_user.id

        expert = Expert(
            id=str(uuid.uuid4()),
            business_id=business_id,
            user_id=user_id,
            name=name,
            email=email,
            skills=[direction],
            status=status,
            active_lead_count=0,
            auto_dial_enabled=False,
        )
        db.add(expert)
        await db.commit()
        await db.refresh(expert)
        expert.agent_type = direction
        expert.direction = direction
        expert.active_calls = 0
        expert.calls_handled = 0
        expert.total_leads = 0
        return expert
    except Exception as exc:
        logger.error(f"Failed to create voice agent: {exc}\n{traceback.format_exc()}")
        await db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to create voice agent: {str(exc)}")


@router.get("/pd-status", response_model=dict)
async def get_active_pd_status(
    agent_id: Optional[str] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        biz_id = user.business_id
        
        # If agent_id not provided, check if current user is an expert
        target_expert = None
        if agent_id:
            target_expert = await db.get(Expert, agent_id)
        else:
            target_expert = (await db.scalars(select(Expert).where(Expert.user_id == user.id))).first()

        # Query all experts to count active PD
        exp_query = select(Expert)
        if biz_id:
            exp_query = exp_query.where(Expert.business_id == biz_id)
        all_experts = list((await db.scalars(exp_query)).all())
        active_pd_agents = [e for e in all_experts if getattr(e, "auto_dial_enabled", False)]

        # Campaign fallback
        camp_query = select(Campaign)
        if biz_id:
            camp_query = camp_query.where(Campaign.business_id == biz_id)
        camp_query = camp_query.order_by(Campaign.created_at.desc())
        camps = (await db.scalars(camp_query)).all()
        active_camp = next((c for c in camps if getattr(c, "auto_dial_enabled", False) and c.status == "RUNNING"), None)
        if not active_camp and camps:
            active_camp = camps[0]

        agent_pd_active = bool(getattr(target_expert, "auto_dial_enabled", False)) if target_expert else bool(len(active_pd_agents) > 0)

        return {
            "status": "ACTIVE" if agent_pd_active else "INACTIVE",
            "auto_dial_enabled": agent_pd_active,
            "agent_id": target_expert.id if target_expert else None,
            "active_agents_count": len(active_pd_agents),
            "total_agents_count": len(all_experts),
            "campaign_status": active_camp.status if active_camp else "IDLE",
            "ratio": getattr(active_camp, "auto_dial_ratio", 1) or 1 if active_camp else 1,
            "campaign_id": active_camp.id if active_camp else None,
            "campaign_name": active_camp.name if active_camp else None,
            "active_calls": 0,
        }
    except Exception as exc:
        logger.error(f"Error in get_active_pd_status: {exc}\n{traceback.format_exc()}")
        return {
            "status": "INACTIVE",
            "auto_dial_enabled": False,
            "agent_id": agent_id,
            "active_agents_count": 0,
            "total_agents_count": 0,
            "campaign_status": "IDLE",
            "ratio": 1,
            "campaign_id": None,
            "campaign_name": None,
            "active_calls": 0,
        }


@router.post("/pd-toggle", response_model=dict)
async def toggle_active_pd_status(
    body: Optional[dict] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    biz_id = user.business_id
    agent_id = body.get("agent_id") if body else None
    
    target_expert = None
    if agent_id:
        target_expert = await db.get(Expert, agent_id)
    else:
        # Check if current user is an expert
        target_expert = (await db.scalars(select(Expert).where(Expert.user_id == user.id))).first()

    if target_expert:
        # Toggle specifically for this agent!
        new_state = body.get("enable") if (body and "enable" in body) else not bool(target_expert.auto_dial_enabled)
        target_expert.auto_dial_enabled = bool(new_state)

        # Synchronize any campaigns assigned strictly to this agent
        camp_stmt = select(Campaign).where(Campaign.voice_agent_id == target_expert.id)
        camps = (await db.scalars(camp_stmt)).all()
        for camp in camps:
            camp.auto_dial_enabled = target_expert.auto_dial_enabled
            if target_expert.auto_dial_enabled:
                camp.status = "RUNNING"
                run_stmt = select(DialerRun).where(DialerRun.campaign_id == camp.id, DialerRun.status == "RUNNING")
                run = (await db.scalars(run_stmt)).first()
                if not run:
                    db.add(DialerRun(campaign_id=camp.id, status="RUNNING"))
            else:
                camp.status = "PAUSED"
                run_stmt = select(DialerRun).where(DialerRun.campaign_id == camp.id, DialerRun.status == "RUNNING")
                run = (await db.scalars(run_stmt)).first()
                if run:
                    run.status = "PAUSED"

        await db.commit()
        await db.refresh(target_expert)

        # Broadcast WebSocket event
        try:
            from app.api.routes.websockets import global_ws_manager
            await global_ws_manager.broadcast({
                "event": "voice_agent_pd_updated",
                "type": "voice_agent_pd_updated",
                "voice_agent_id": target_expert.id,
                "agent_id": target_expert.id,
                "auto_dial_enabled": target_expert.auto_dial_enabled,
                "business_id": target_expert.business_id or "default"
            })
        except Exception as ws_err:
            logger.warning(f"WebSocket broadcast error on agent pd-toggle: {ws_err}")

        # Count active agents
        exp_query = select(Expert)
        if biz_id:
            exp_query = exp_query.where(Expert.business_id == biz_id)
        all_experts = list((await db.scalars(exp_query)).all())
        active_pd_agents = [e for e in all_experts if getattr(e, "auto_dial_enabled", False)]

        return {
            "message": f"Predictive Dialer {'activated' if target_expert.auto_dial_enabled else 'deactivated'} for {target_expert.name}",
            "status": "ACTIVE" if target_expert.auto_dial_enabled else "INACTIVE",
            "auto_dial_enabled": target_expert.auto_dial_enabled,
            "agent_id": target_expert.id,
            "agent_name": target_expert.name,
            "active_agents_count": len(active_pd_agents),
            "total_agents_count": len(all_experts),
        }

    # If no target expert found, fallback to campaign-based toggle
    query = select(Campaign)
    if biz_id:
        query = query.where(Campaign.business_id == biz_id)
    query = query.order_by(Campaign.created_at.desc())
    camps = (await db.scalars(query)).all()
    
    active_camp = next((c for c in camps if c.auto_dial_enabled and c.status == "RUNNING"), None)
    if not active_camp and camps:
        active_camp = camps[0]
        
    if not active_camp:
        active_camp = Campaign(
            id=str(uuid.uuid4()),
            business_id=biz_id or "",
            name="Default Voice Agent Campaign",
            status="IDLE",
            auto_dial_enabled=False,
            auto_dial_ratio=1,
            twilio_number="+15550100",
        )
        db.add(active_camp)
        await db.flush()

    is_currently_active = bool(active_camp.auto_dial_enabled and active_camp.status == "RUNNING")
    new_state = body.get("enable") if (body and "enable" in body) else not is_currently_active

    if new_state:
        active_camp.status = "RUNNING"
        active_camp.auto_dial_enabled = True
        stmt = select(DialerRun).where(DialerRun.campaign_id == active_camp.id, DialerRun.status == "RUNNING")
        run = (await db.scalars(stmt)).first()
        if not run:
            run = DialerRun(campaign_id=active_camp.id, status="RUNNING")
            db.add(run)
    else:
        for c in camps:
            if c.auto_dial_enabled or c.status == "RUNNING":
                c.status = "PAUSED"
                c.auto_dial_enabled = False
                stmt = select(DialerRun).where(DialerRun.campaign_id == c.id, DialerRun.status == "RUNNING")
                run = (await db.scalars(stmt)).first()
                if run:
                    run.status = "PAUSED"

    await db.commit()
    await db.refresh(active_camp)

    try:
        from app.api.routes.websockets import global_ws_manager
        await global_ws_manager.broadcast({
            "event": "campaign_status_changed",
            "campaign_id": str(active_camp.id),
            "status": str(active_camp.status),
            "business_id": str(active_camp.business_id or "default")
        })
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on pd-toggle: {ws_err}")

    is_now_active = bool(active_camp.auto_dial_enabled and active_camp.status == "RUNNING")

    return {
        "message": f"Predictive Dialer {'activated' if is_now_active else 'deactivated'}",
        "status": "ACTIVE" if is_now_active else "INACTIVE",
        "auto_dial_enabled": is_now_active,
        "campaign_id": active_camp.id,
    }


@router.get("/{id}", response_model=ExpertOut)
async def get_voice_agent(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    return expert


@router.put("/{id}", response_model=ExpertOut)
async def update_voice_agent(
    id: str,
    body: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")

    if "name" in body and body["name"]:
        expert.name = body["name"]
    if "email" in body:
        expert.email = body["email"]
    if "status" in body:
        expert.status = body["status"]
    if "direction" in body or "agent_type" in body:
        new_dir = (body.get("direction") or body.get("agent_type") or "INBOUND").upper().strip()
        expert.skills = [new_dir]
        if expert.user_id:
            u = await db.get(User, expert.user_id)
            if u:
                perms = dict(u.permissions or {})
                perms["agent_type"] = new_dir
                u.permissions = perms
                db.add(u)
    if "auto_dial_enabled" in body:
        expert.auto_dial_enabled = bool(body["auto_dial_enabled"])

    await db.commit()
    await db.refresh(expert)
    return expert


@router.patch("/{id}/pd", response_model=ExpertOut)
async def toggle_voice_agent_pd(
    id: str,
    body: Optional[dict] = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")

    new_state = body.get("auto_dial_enabled") if (body and "auto_dial_enabled" in body) else not bool(expert.auto_dial_enabled)
    expert.auto_dial_enabled = bool(new_state)

    # Synchronize any campaigns assigned to this agent
    camp_stmt = select(Campaign).where(Campaign.voice_agent_id == expert.id)
    camps = (await db.scalars(camp_stmt)).all()
    for camp in camps:
        camp.auto_dial_enabled = expert.auto_dial_enabled
        if expert.auto_dial_enabled:
            camp.status = "RUNNING"
            run_stmt = select(DialerRun).where(DialerRun.campaign_id == camp.id, DialerRun.status == "RUNNING")
            run = (await db.scalars(run_stmt)).first()
            if not run:
                db.add(DialerRun(campaign_id=camp.id, status="RUNNING"))
        else:
            camp.status = "PAUSED"
            run_stmt = select(DialerRun).where(DialerRun.campaign_id == camp.id, DialerRun.status == "RUNNING")
            run = (await db.scalars(run_stmt)).first()
            if run:
                run.status = "PAUSED"

    await db.commit()
    await db.refresh(expert)

    # Broadcast WebSocket update
    try:
        from app.api.routes.websockets import global_ws_manager
        ws_payload = {
            "event": "voice_agent_pd_updated",
            "type": "voice_agent_pd_updated",
            "voice_agent_id": expert.id,
            "agent_id": expert.id,
            "auto_dial_enabled": expert.auto_dial_enabled,
            "business_id": expert.business_id or "default",
        }
        await global_ws_manager.broadcast(ws_payload)
    except Exception as ws_err:
        logger.warning(f"WebSocket broadcast error on agent pd toggle: {ws_err}")

    return expert


@router.patch("/{id}/status", response_model=ExpertOut)
async def update_voice_agent_status(
    id: str,
    body: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")

    raw_status = body.get("status")
    if not raw_status:
        raise HTTPException(status_code=400, detail="Missing status field in request body")

    valid_statuses = {"AVAILABLE", "BUSY", "OFFLINE", "FREE", "ON_CALL", "PAUSED"}
    norm_status = raw_status.upper().strip()
    if norm_status not in valid_statuses:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid status '{raw_status}'. Must be one of: AVAILABLE, BUSY, OFFLINE"
        )

    new_status = norm_status
    expert.status = new_status
    if expert.user_id:
        user_obj = await db.get(User, expert.user_id)
        if user_obj:
            user_obj.status = new_status
            db.add(user_obj)
    await db.commit()
    await db.refresh(expert)

    # Real-time WebSocket event emission
    try:
        from app.api.routes.websockets import global_ws_manager
        ws_payload = {
            "event": "voice_agent_status_updated",
            "type": "voice_agent_status_updated",
            "voice_agent_id": expert.id,
            "agent_id": expert.id,
            "user_id": expert.user_id,
            "status": new_status,
            "business_id": expert.business_id
        }
        await global_ws_manager.broadcast(ws_payload)
        if expert.business_id:
            await global_ws_manager.broadcast_to_business(expert.business_id, ws_payload)
    except Exception as ws_err:
        logger.warning(f"[WS ERROR] Failed to emit voice_agent_status_updated event: {ws_err}")

    if new_status in ("AVAILABLE", "FREE"):
        try:
            from app.services.lead_assignment_service import LeadAssignmentService
            await LeadAssignmentService.process_assignment_queue(db, business_id=user.business_id or "")
        except Exception as e:
            logger.error(f"[QUEUE CHECK ERROR] Failed to process queue on voice agent status update: {e}")

    return expert


@router.patch("/{id}/direction", response_model=ExpertOut)
async def update_voice_agent_direction(
    id: str,
    body: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")

    raw_dir = body.get("direction") or body.get("agent_type") or "INBOUND"
    norm_dir = raw_dir.upper().strip()
    if norm_dir not in ["INBOUND", "OUTBOUND", "ADMIN"]:
        norm_dir = "INBOUND"

    expert.skills = [norm_dir]
    if expert.user_id:
        u = await db.get(User, expert.user_id)
        if u:
            perms = dict(u.permissions or {})
            perms["agent_type"] = norm_dir
            u.permissions = perms
            db.add(u)
    elif expert.email:
        stmt_u = select(User).where(User.email == expert.email)
        u = (await db.scalars(stmt_u)).first()
        if u:
            perms = dict(u.permissions or {})
            perms["agent_type"] = norm_dir
            u.permissions = perms
            db.add(u)

    db.add(expert)
    await db.commit()
    await db.refresh(expert)

    expert.agent_type = norm_dir
    expert.direction = norm_dir

    # Real-time WebSocket event emission so all connected screens update
    try:
        from app.api.routes.websockets import global_ws_manager
        ws_payload = {
            "event": "voice_agent_status_updated",
            "type": "voice_agent_status_updated",
            "voice_agent_id": expert.id,
            "agent_id": expert.id,
            "user_id": expert.user_id,
            "agent_type": norm_dir,
            "direction": norm_dir,
            "status": expert.status,
            "business_id": expert.business_id,
        }
        await global_ws_manager.broadcast(ws_payload)
        if expert.business_id:
            await global_ws_manager.broadcast_to_business(expert.business_id, ws_payload)
    except Exception as ws_err:
        logger.warning(f"[WS ERROR] Failed to emit voice_agent_direction_updated event: {ws_err}")

    return expert


@router.delete("/{id}")
async def delete_voice_agent(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        expert = await db.get(Expert, id)
        if not expert:
            raise HTTPException(status_code=404, detail="Voice agent not found")
        if expert.business_id and user.business_id and expert.business_id != user.business_id:
            raise HTTPException(status_code=404, detail="Voice agent not found")

        # Unassign leads
        leads_res = await db.execute(select(Lead).where(Lead.assigned_expert_id == id))
        for lead in leads_res.scalars().all():
            lead.assigned_expert_id = None

        # Unassign deals
        deals_res = await db.execute(select(Deal).where(Deal.expert_id == id))
        for deal in deals_res.scalars().all():
            deal.expert_id = None

        # Unassign appointments
        appts_res = await db.execute(select(Appointment).where(Appointment.expert_id == id))
        for appt in appts_res.scalars().all():
            appt.expert_id = None

        # Delete LeadAssignments
        await db.execute(delete(LeadAssignment).where(LeadAssignment.expert_id == id))

        user_id_to_delete = expert.user_id

        # Delete Expert first (because it references User)
        await db.delete(expert)
        await db.flush()

        # Delete associated user if exists
        if user_id_to_delete:
            from app.db.models import (
                UserInvitation,
                EmailVerificationToken,
                PasswordResetToken,
                Notification,
            )
            await db.execute(delete(UserInvitation).where(UserInvitation.user_id == user_id_to_delete))
            await db.execute(delete(EmailVerificationToken).where(EmailVerificationToken.user_id == user_id_to_delete))
            await db.execute(delete(PasswordResetToken).where(PasswordResetToken.user_id == user_id_to_delete))
            await db.execute(delete(Notification).where(Notification.user_id == user_id_to_delete))

            u_res = await db.get(User, user_id_to_delete)
            if u_res:
                await db.delete(u_res)

        await db.commit()
        return {"success": True, "message": "Voice agent deleted successfully"}
    except Exception as e:
        trace = traceback.format_exc()
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}\n\nTraceback: {trace}")


@router.get("/{id}/overview")
async def get_voice_agent_overview(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    expert = await db.get(Expert, id)
    if not expert:
        raise HTTPException(status_code=404, detail="Voice agent not found")
    if expert.business_id and user.business_id and expert.business_id != user.business_id:
        raise HTTPException(status_code=404, detail="Voice agent not found")

    # Find assigned campaigns
    camps_res = await db.execute(
        select(Campaign).where(
            or_(
                Campaign.voice_agent_id == expert.id,
                Campaign.voice_agent_id == expert.user_id,
                and_(Campaign.voice_agent_id.isnot(None), Campaign.voice_agent_id.like(f"%{expert.id}%"))
            )
        )
    )
    assigned_camps = camps_res.scalars().all()
    camp_ids = [c.id for c in assigned_camps]

    call_conds = [Call.agent_id == expert.id]
    if expert.user_id:
        call_conds.append(Call.agent_id == expert.user_id)
    if camp_ids:
        call_conds.append(Call.campaign_id.in_(camp_ids))

    ACTIVE_CALL_STATUSES = [
        "IN_PROGRESS", "RINGING", "CLIENT_ADDING", "CONFERENCE_3WAY_ACTIVE",
        "HUMAN_HANDLING", "TRANSFER_REQUESTED", "DIALING", "CONNECTED", "TALKING", "ON_CALL",
        "CALLING", "IN_CALL", "RESERVED", "AI_HANDLING", "QUEUED", "ACTIVE", "LIVE", "BRIDGED",
        "initiating", "ringing", "answered", "in_progress"
    ]
    HANDLED_CALL_STATUSES = [
        "COMPLETED", "QUALIFIED", "NOT_QUALIFIED", "HUMAN_HANDLING",
        "CALLBACK_REQUIRED", "ANSWERED", "CLOSED", "TRANSFERRED", "SALE_SUCCESS", "IN_PROGRESS"
    ]

    stmt_active = select(func.count(Call.id)).where(
        and_(
            or_(*call_conds),
            Call.status.in_(ACTIVE_CALL_STATUSES)
        )
    )
    active_calls_cnt = (await db.execute(stmt_active)).scalar() or 0

    stmt_handled = select(func.count(Call.id)).where(
        and_(
            or_(*call_conds),
            or_(
                Call.status.in_(HANDLED_CALL_STATUSES),
                (Call.duration_seconds.isnot(None) & (Call.duration_seconds > 0))
            )
        )
    )
    handled_calls_cnt = (await db.execute(stmt_handled)).scalar() or 0

    stmt_qual = select(func.count(Call.id)).where(
        and_(
            or_(*call_conds),
            Call.status == "QUALIFIED"
        )
    )
    qual_calls_cnt = (await db.execute(stmt_qual)).scalar() or 0

    direct_leads_stmt = select(func.count(Lead.id)).where(
        or_(Lead.assigned_expert_id == expert.id, Lead.assigned_expert_id == expert.user_id) if expert.user_id else (Lead.assigned_expert_id == expert.id)
    )
    direct_leads_cnt = (await db.execute(direct_leads_stmt)).scalar() or 0
    camp_leads_cnt = 0
    if camp_ids:
        camp_leads_cnt = (await db.execute(select(func.count(CampaignLead.id)).where(CampaignLead.campaign_id.in_(camp_ids)))).scalar() or 0
    total_leads_cnt = direct_leads_cnt + camp_leads_cnt

    expert.active_calls = active_calls_cnt
    expert.calls_handled = handled_calls_cnt
    expert.total_leads = total_leads_cnt
    expert.active_lead_count = active_calls_cnt if active_calls_cnt > 0 else handled_calls_cnt
    if active_calls_cnt > 0:
        expert.status = "BUSY"

    return {
        "agent": ExpertOut.model_validate(expert),
        "total_campaigns": len(assigned_camps),
        "total_leads_worked": total_leads_cnt,
        "qualified_calls": qual_calls_cnt,
        "calls_handled": handled_calls_cnt,
        "active_calls": active_calls_cnt,
    }


@router.get("/{id}/campaigns", response_model=List[CampaignOut])
async def get_voice_agent_campaigns(
    id: str,
    status: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    query = select(Campaign).where(Campaign.business_id == business_id)
    if status and status != "ALL":
        query = query.where(Campaign.status == status)
    result = await db.execute(query)
    camps = result.scalars().all()
    return list(camps)


@router.get("/{id}/leads", response_model=List[LeadOut])
async def get_voice_agent_leads(
    id: str,
    search: Optional[str] = None,
    call_status: Optional[str] = None,
    lead_status: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    query = select(Lead).where(Lead.assigned_expert_id == id)
    if call_status:
        query = query.where(Lead.call_status == call_status)
    if lead_status:
        query = query.where(Lead.qualification_status == lead_status)
    result = await db.execute(query)
    leads = result.scalars().all()
    return list(leads)


@router.get("/{id}/qualified-calls", response_model=List[LeadOut])
async def get_voice_agent_qualified_calls(
    id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Lead).where(Lead.assigned_expert_id == id, Lead.qualification_status == "QUALIFIED"))
    leads = result.scalars().all()
    return list(leads)


@router.get("/{id}/call-history")
async def get_voice_agent_call_history(
    id: str,
    range_filter: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Lead).where(Lead.assigned_expert_id == id))
    leads = result.scalars().all()
    history = []
    for l in leads:
        history.append({
            "id": l.id,
            "lead_name": f"{l.first_name} {l.last_name}".strip() or l.email,
            "phone": l.phone,
            "call_status": l.call_status or "COMPLETED",
            "qualification_status": l.qualification_status,
            "date": l.call_date_time.isoformat() if l.call_date_time else l.created_at.isoformat(),
            "duration": l.call_duration or 0,
            "summary": l.ai_summary or "",
        })
    return history



