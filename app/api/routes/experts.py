import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from datetime import datetime, timezone
from pydantic import BaseModel

from app.core.deps import get_current_business_id, get_current_db_user
from app.db.database import get_db
from app.db.models import Expert, Lead, User, Call, Campaign, CampaignLead
from app.schemas.expert import ExpertCreate, ExpertOut

router = APIRouter(prefix="/api/experts", tags=["experts"])


class StatusUpdate(BaseModel):
    status: str


class CallCompletePayload(BaseModel):
    voice_notes: str = ""
    final_outcome: str = "Completed"
    lead_status: str = "QUALIFIED"


@router.post("", response_model=ExpertOut, status_code=201)
async def create_expert(
    payload: ExpertCreate,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Expert:
    expert = Expert(business_id=business_id, **payload.model_dump())
    db.add(expert)
    await db.commit()
    await db.refresh(expert)
    return expert


@router.get("", response_model=list[ExpertOut])
async def list_experts(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business
    try:
        biz_id = user.business_id
        if not biz_id:
            biz_row = (await db.execute(select(Business))).scalars().first()
            if biz_row:
                biz_id = biz_row.id
            else:
                biz = Business(name="CallZenza Primary Business", owner_user_id=user.id)
                db.add(biz)
                await db.flush()
                biz_id = biz.id

        va_stmt = select(User).where(func.upper(User.role).in_(["VOICE_AGENT", "AGENT"]))
        if biz_id:
            va_stmt = va_stmt.where(User.business_id == biz_id)
        va_users_res = await db.scalars(va_stmt)
        for va_user in va_users_res.all():
            exp_check = await db.scalar(
                select(Expert).where(or_(Expert.user_id == va_user.id, Expert.email == va_user.email))
            )
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
                if va_user.full_name and exp_check.name != va_user.full_name:
                    exp_check.name = va_user.full_name
        await db.commit()
    except Exception as exc:
        from loguru import logger
        logger.warning(f"Note on auto-seeding experts: {exc}")
        await db.rollback()

    try:
        exp_query = select(Expert).order_by(Expert.created_at.desc())
        if biz_id:
            exp_query = exp_query.where(Expert.business_id == biz_id)
        result = await db.scalars(exp_query)
        experts = list(result.all())
        filtered_experts = []
        for e in experts:
            u = None
            if e.user_id:
                u = await db.get(User, e.user_id)
            if not u and e.email:
                u = (await db.scalars(select(User).where(User.email == e.email))).first()

            # Strictly exclude admins, supervisors, business owners, or standard users
            if u and u.role and u.role.upper() not in ["VOICE_AGENT", "AGENT", "INBOUND_AGENT", "OUTBOUND_AGENT"]:
                continue
            if e.email and any(adm in e.email.lower() for adm in ["admin@callzenza.com", "admin@callmira.com", "superadmin@", "superv@gmail.com"]):
                continue

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

            skills_upper = [str(s).upper() for s in (e.skills or [])]

            computed_type = "OUTBOUND"
            if u and u.role in ["SUPER_ADMIN", "ADMIN"]:
                continue
            elif "INBOUND" in skills_upper or (u and u.permissions and u.permissions.get("agent_type") == "INBOUND") or (u and u.role == "INBOUND_AGENT"):
                computed_type = "INBOUND"
            elif "OUTBOUND" in skills_upper or (u and u.permissions and u.permissions.get("agent_type") == "OUTBOUND") or (u and u.role == "OUTBOUND_AGENT"):
                computed_type = "OUTBOUND"
            elif "ADMIN" in skills_upper:
                continue
            else:
                computed_type = "INBOUND" if "INBOUND" in skills_upper else "OUTBOUND"

            e.agent_type = computed_type
            e.direction = computed_type
            filtered_experts.append(e)
        return filtered_experts
    except Exception as exc:
        from loguru import logger
        logger.error(f"Error in list_experts: {exc}")
        return []


@router.patch("/{id}/direction", response_model=ExpertOut)
async def update_expert_direction(
    id: str,
    body: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.api.routes.voice_agents import update_voice_agent_direction
    return await update_voice_agent_direction(id=id, body=body, user=user, db=db)


@router.get("/me", response_model=ExpertOut)
async def get_my_expert_profile(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    from app.db.models import Business
    try:
        expert = await db.scalar(
            select(Expert).where(or_(Expert.user_id == user.id, Expert.email == user.email))
        )
        if not expert:
            biz_id = user.business_id
            if not biz_id:
                biz_row = (await db.execute(select(Business))).scalars().first()
                if biz_row:
                    biz_id = biz_row.id
                else:
                    biz = Business(name="CallZenza Primary Business", owner_user_id=user.id)
                    db.add(biz)
                    await db.flush()
                    biz_id = biz.id

            expert = Expert(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                user_id=user.id,
                name=user.full_name or user.email,
                email=user.email,
                status="AVAILABLE",
                skills=[],
                active_lead_count=0
            )
            db.add(expert)
            await db.commit()
            await db.refresh(expert)
        return expert
    except Exception as exc:
        from loguru import logger
        logger.warning(f"Note retrieving my expert profile: {exc}")
        await db.rollback()
        biz_fallback = user.business_id or "default-biz"
        return Expert(
            id=user.id,
            business_id=biz_fallback,
            user_id=user.id,
            name=user.full_name or user.email,
            email=user.email,
            status="AVAILABLE",
            skills=[],
            active_lead_count=0,
            auto_dial_enabled=False
        )


@router.patch("/me/status", response_model=ExpertOut)
async def update_my_status(
    payload: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Expert:
    new_status = payload.get("status", "AVAILABLE")
    expert = await db.scalar(select(Expert).where(Expert.user_id == user.id))
    if not expert:
        if user.role in ("ADMIN", "SUPER_ADMIN"):
            return Expert(
                id="admin-mock-expert",
                business_id="00000000-0000-0000-0000-000000000000",
                user_id=user.id,
                name=user.full_name or user.email.split("@")[0],
                email=user.email,
                status=new_status,
                skills=[],
                active_lead_count=0,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None)
            )
        raise HTTPException(status_code=404, detail="Expert profile not found")
    expert.status = new_status
    user.status = new_status
    db.add(user)
    await db.commit()
    await db.refresh(expert)

    if new_status in ("AVAILABLE", "FREE"):
        try:
            from app.services.lead_assignment_service import LeadAssignmentService
            await LeadAssignmentService.process_assignment_queue(db, business_id=expert.business_id)
        except Exception as e:
            from loguru import logger
            logger.error(f"[QUEUE CHECK ERROR] Failed to process queue on status update: {e}")

    return expert


@router.get("/me/leads")
async def get_my_leads(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> list[Any]:
    expert = await db.scalar(select(Expert).where(Expert.user_id == user.id))
    if not expert:
        return []
    result = await db.scalars(select(Lead).where(Lead.assigned_expert_id == expert.id))
    return list(result)


@router.get("/me/calls")
async def get_my_calls(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> list[Any]:
    expert = await db.scalar(select(Expert).where(Expert.user_id == user.id))
    if not expert:
        return []
    leads_res = await db.execute(select(Lead).where(Lead.assigned_expert_id == expert.id))
    leads = leads_res.scalars().all()
    calls = []
    for l in leads:
        calls.append({
            "id": l.id,
            "lead_id": l.id,
            "lead_name": f"{l.first_name} {l.last_name}".strip() or l.email,
            "phone": l.phone,
            "status": l.call_status or "COMPLETED",
            "date": l.call_date_time.isoformat() if l.call_date_time else l.created_at.isoformat(),
            "duration": l.call_duration or 0,
            "result": l.call_result or "QUALIFIED",
            "summary": l.ai_summary or "",
        })
    return calls


@router.post("/me/calls/{call_id}/complete")
async def complete_my_call(
    call_id: str,
    payload: dict,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    lead = await db.get(Lead, call_id)
    if lead:
        lead.call_status = "COMPLETED"
        lead.qualification_status = payload.get("lead_status", "QUALIFIED")
        lead.call_result = payload.get("final_outcome", "Completed")
        if payload.get("voice_notes"):
            lead.ai_summary = f"{lead.ai_summary}\n\nExpert Notes: {payload.get('voice_notes')}".strip()
        await db.commit()
    return {"success": True, "message": "Call completed successfully"}


@router.get("/{expert_id}/leads")
async def get_expert_leads(
    expert_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> list[Any]:
    result = await db.scalars(
        select(Lead)
        .where(Lead.business_id == business_id)
        .where(Lead.assigned_expert_id == expert_id)
    )
    leads = []
    for lead in result:
        leads.append({
            "id": lead.id,
            "first_name": lead.first_name,
            "last_name": lead.last_name,
            "phone": lead.phone,
            "company": lead.company,
        })
    return leads


@router.get("/{expert_id}", response_model=ExpertOut)
async def get_expert(
    expert_id: str,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Expert:
    expert = await db.get(Expert, expert_id)
    if not expert or expert.business_id != business_id:
        raise HTTPException(status_code=404, detail="Expert not found")
    return expert
