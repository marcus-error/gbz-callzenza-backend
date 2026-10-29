from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.deps import get_current_business_id, get_current_db_user
from app.db.database import get_db
from app.db.models import AgentAssignmentQueue, Expert, Lead, LeadAssignment, User, Campaign, CampaignLead, Client, Call, CallTransfer
from app.schemas.lead_assignment import (
    CompleteAICallRequest,
    LeadAssignmentOut,
    QueueItemOut,
    TransferToAgentRequest,
    TransferToAgentResponse,
)
from app.services.lead_assignment_service import LeadAssignmentService

router = APIRouter(prefix="/api/lead-assignment", tags=["Lead Assignment & Transfer"])


@router.post("/transfer-to-agent", response_model=TransferToAgentResponse)
async def transfer_to_agent(
    payload: TransferToAgentRequest,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Triggered when "Talk to Agent" or a specific agent request is initiated:
    - Finds a FREE/AVAILABLE human voice agent and assigns the lead.
    - If a specific agent is requested and busy, places into requested agent's queue.
    - Prevents duplicate active assignments.
    """
    try:
        res = await LeadAssignmentService.transfer_lead_to_agent(
            db=db,
            business_id=business_id,
            lead_id=payload.lead_id,
            call_id=payload.call_id,
            campaign_id=payload.campaign_id,
            ai_agent_name=payload.ai_agent_name or "Voice Agent AI",
            qualification_stage=payload.qualification_stage or "STAGING",
            transcript=payload.transcript or "",
            notes=payload.notes or "",
            collected_answers=payload.collected_answers,
            requested_expert_name=payload.requested_expert_name,
        )
        return res
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))
    except Exception as e:
        logger.error(f"[TRANSFER ERROR] Failed to process transfer: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.post("/complete-ai-call", response_model=TransferToAgentResponse)
async def complete_ai_call(
    payload: CompleteAICallRequest,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """
    Triggered upon successful completion of an AI Voice Agent call:
    - Checks if the lead is already assigned to a human voice agent.
    - If not assigned, automatically assigns to a free agent or queues the lead.
    """
    try:
        res = await LeadAssignmentService.complete_ai_call_assignment(
            db=db,
            business_id=business_id,
            lead_id=payload.lead_id,
            call_id=payload.call_id,
            qualification_status=payload.qualification_status or "QUALIFIED",
            summary=payload.summary or "",
            transcript=payload.transcript or "",
            notes=payload.notes or "",
        )
        return res
    except ValueError as ve:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(ve))
    except Exception as e:
        logger.error(f"[AI CALL COMPLETE ERROR] Failed to process assignment: {e}")
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))


@router.get("/queue", response_model=List[QueueItemOut])
async def list_assignment_queue(
    status_filter: Optional[str] = "WAITING",
    expert_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns all queued leads waiting for human agent assignment."""
    query = (
        select(AgentAssignmentQueue, Lead)
        .outerjoin(Lead, AgentAssignmentQueue.lead_id == Lead.id)
    )
    if status_filter:
        if status_filter == "WAITING":
            query = query.where(AgentAssignmentQueue.status.in_(["WAITING", "ASSIGNED", "IN_PROGRESS"]))
        elif status_filter == "HISTORY":
            query = query.where(AgentAssignmentQueue.status.in_(["COMPLETED", "REJECTED", "CANCELLED", "FAILED", "TRANSFERRED"]))
        else:
            query = query.where(AgentAssignmentQueue.status == status_filter)
    else:
        query = query.where(AgentAssignmentQueue.status.in_(["WAITING", "ASSIGNED", "IN_PROGRESS"]))
    if expert_id:
        query = query.where(AgentAssignmentQueue.requested_expert_id == expert_id)
    if campaign_id:
        query = query.where(AgentAssignmentQueue.campaign_id == campaign_id)
    
    query = query.order_by(AgentAssignmentQueue.created_at.asc())
    res = await db.execute(query)
    rows = res.all()

    items = []
    for q_item, lead in rows:
        req_exp_name = None
        if q_item.requested_expert_id:
            req_exp = await db.get(Expert, q_item.requested_expert_id)
            if req_exp:
                req_exp_name = req_exp.name

        items.append({
            "id": q_item.id,
            "business_id": q_item.business_id,
            "lead_id": q_item.lead_id,
            "lead_name": (f"{lead.first_name} {lead.last_name}".strip() if lead else "") or (lead.email if lead else "Unknown"),
            "phone": lead.phone if lead else "",
            "email": lead.email if lead else "",
            "company": lead.company if lead else "",
            "requested_expert_id": q_item.requested_expert_id,
            "requested_expert_name": req_exp_name,
            "trigger_source": q_item.trigger_source,
            "reason": q_item.reason,
            "status": q_item.status,
            "created_at": q_item.created_at,
            "assigned_at": q_item.assigned_at,
        })
    return items


from datetime import datetime, timezone

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.post("/queue/{queue_id}/assign")
async def assign_queue_item(
    queue_id: str,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Transition a queue item to ASSIGNED status with row locking to prevent race conditions."""
    stmt = select(AgentAssignmentQueue).where(AgentAssignmentQueue.id == queue_id).with_for_update()
    res = await db.execute(stmt)
    q_item = res.scalars().first()
    if not q_item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    if q_item.status in ("ASSIGNED", "COMPLETED", "IN_PROGRESS"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Lead queue item is already {q_item.status}."
        )

    q_item.status = "ASSIGNED"
    q_item.assigned_agent_id = current_user.id
    q_item.assigned_at = _now()
    await db.commit()
    return {"success": True, "status": q_item.status}


@router.post("/queue/{queue_id}/start")
async def start_queue_item(
    queue_id: str,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Transition a queue item to IN_PROGRESS status."""
    q_item = await db.get(AgentAssignmentQueue, queue_id)
    if not q_item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    q_item.status = "IN_PROGRESS"
    q_item.call_started_at = _now()

    current_user.status = "BUSY"
    db.add(current_user)

    await db.commit()
    return {"success": True, "status": q_item.status}


@router.post("/queue/{queue_id}/complete")
async def complete_queue_item(
    queue_id: str,
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """Transition a queue item to COMPLETED status."""
    q_item = await db.get(AgentAssignmentQueue, queue_id)
    if not q_item:
        raise HTTPException(status_code=404, detail="Queue item not found")

    q_item.status = "COMPLETED"
    q_item.call_completed_at = _now()
    if q_item.call_started_at:
        delta = q_item.call_completed_at - q_item.call_started_at
        q_item.call_duration = int(delta.total_seconds())

    current_user.status = "AVAILABLE"
    db.add(current_user)

    await db.commit()
    return {"success": True, "status": q_item.status}


@router.post("/process-queue")
async def process_queue(
    expert_id: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Triggers queue evaluation to assign waiting leads to available agents with DB locking protection."""
    try:
        count = await LeadAssignmentService.process_assignment_queue(db, business_id=business_id, expert_id=expert_id)
        return {"success": True, "assigned_count": count, "message": f"Processed queue: {count} leads assigned"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[PROCESS QUEUE CONFLICT] Race condition or database lock error: {e}")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Queue processing conflict. Lead is locked or currently being assigned by another process."
        )


@router.get("/assignments", response_model=List[LeadAssignmentOut])
async def list_assignments(
    trigger_source: Optional[str] = None,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Lists active and historical lead assignment records with rich context."""
    try:
        query = (
            select(LeadAssignment, Expert)
            .join(Expert, LeadAssignment.expert_id == Expert.id)
            .where(LeadAssignment.business_id == business_id)
        )
        if trigger_source:
            query = query.where(LeadAssignment.trigger_source == trigger_source)

        query = query.order_by(LeadAssignment.assigned_at.desc())
        res = await db.execute(query)
        rows = res.all()

        assignments = []
        for assign, expert in rows:
            prev_name = None
            if assign.previous_expert_id:
                prev_exp = await db.get(Expert, assign.previous_expert_id)
                if prev_exp:
                    prev_name = prev_exp.name
            elif assign.context and assign.context.get("previous_expert_name"):
                prev_name = assign.context.get("previous_expert_name")

            assignments.append({
                "id": assign.id,
                "business_id": assign.business_id,
                "lead_id": assign.lead_id,
                "expert_id": assign.expert_id,
                "expert_name": expert.name,
                "expert_email": expert.email,
                "previous_expert_id": assign.previous_expert_id,
                "previous_expert_name": prev_name,
                "strategy": assign.strategy,
                "status": assign.status,
                "trigger_source": assign.trigger_source,
                "ai_agent_name": assign.ai_agent_name,
                "qualification_stage": assign.qualification_stage,
                "transcript": assign.transcript,
                "notes": assign.notes,
                "context": assign.context or {},
                "assigned_at": assign.assigned_at,
            })
        return assignments
    except Exception as exc:
        from loguru import logger
        logger.error(f"Error in list_assignments: {exc}")
        return []


@router.get("/my-assignments", response_model=List[LeadAssignmentOut])
async def list_my_assignments(
    status_filter: Optional[str] = None,
    expert_id: Optional[str] = None,
    current_user: User = Depends(get_current_db_user),
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
):
    """Returns leads assigned to the authenticated human voice agent / expert."""
    target_expert_id = expert_id
    if not target_expert_id:
        exp_res = await db.execute(select(Expert).where(or_(Expert.user_id == current_user.id, Expert.email == current_user.email)))
        user_expert = exp_res.scalars().first()
        if user_expert:
            target_expert_id = user_expert.id

    agent_ids = list(set(filter(None, [current_user.id, target_expert_id])))

    query = (
        select(LeadAssignment, Expert)
        .outerjoin(Expert, LeadAssignment.expert_id == Expert.id)
    )
    if agent_ids:
        query = query.where(or_(LeadAssignment.expert_id.in_(agent_ids), LeadAssignment.business_id == business_id))
    else:
        query = query.where(LeadAssignment.business_id == business_id)

    if status_filter:
        if status_filter == "HISTORY":
            query = query.where(LeadAssignment.status.in_(["REASSIGNED", "COMPLETED", "TRANSFERRED", "REJECTED", "CANCELLED"]))
        else:
            query = query.where(LeadAssignment.status == status_filter)

    query = query.order_by(LeadAssignment.assigned_at.desc())
    res = await db.execute(query)
    rows = res.all()

    assignments = []
    assigned_lead_ids = set()
    for assign, expert in rows:
        assigned_lead_ids.add(assign.lead_id)
        prev_name = None
        if assign.previous_expert_id:
            prev_exp = await db.get(Expert, assign.previous_expert_id)
            if prev_exp:
                prev_name = prev_exp.name
        elif assign.context and assign.context.get("previous_expert_name"):
            prev_name = assign.context.get("previous_expert_name")

        exp_name = expert.name if expert else (current_user.full_name or "Agent")
        exp_email = expert.email if expert else current_user.email

        assignments.append({
            "id": assign.id,
            "business_id": assign.business_id,
            "lead_id": assign.lead_id,
            "expert_id": assign.expert_id or current_user.id,
            "expert_name": exp_name,
            "expert_email": exp_email,
            "previous_expert_id": assign.previous_expert_id,
            "previous_expert_name": prev_name,
            "strategy": assign.strategy,
            "status": assign.status,
            "trigger_source": assign.trigger_source,
            "ai_agent_name": assign.ai_agent_name,
            "qualification_stage": assign.qualification_stage,
            "transcript": assign.transcript,
            "notes": assign.notes,
            "context": assign.context or {},
            "assigned_at": assign.assigned_at,
        })

    # Include CampaignLeads assigned to this expert
    c_leads_stmt = (
        select(CampaignLead, Lead, Campaign)
        .join(Lead, CampaignLead.lead_id == Lead.id)
        .join(Campaign, CampaignLead.campaign_id == Campaign.id)
        .where(
            or_(
                CampaignLead.assigned_expert_id.in_(agent_ids),
                Campaign.voice_agent_id.in_(agent_ids),
                Campaign.agent_id.in_(agent_ids),
                Campaign.business_id == business_id
            )
        )
    )
    if status_filter == "ACTIVE" or not status_filter:
        c_leads_stmt = c_leads_stmt.where(CampaignLead.status.in_(["PENDING", "CALLING", "IN_PROGRESS"]))
    elif status_filter == "HISTORY":
        c_leads_stmt = c_leads_stmt.where(CampaignLead.status.in_(["COMPLETED", "CALLED", "DISQUALIFIED", "FAILED"]))
    
    c_rows = (await db.execute(c_leads_stmt)).all()
    user_exp = await db.get(Expert, target_expert_id) if target_expert_id else None
    exp_name = user_exp.name if user_exp else (current_user.full_name or "Agent")
    exp_email = user_exp.email if user_exp else current_user.email
    for cl, lead, camp in c_rows:
        if lead.id not in assigned_lead_ids:
            assigned_lead_ids.add(lead.id)
            client_obj = await db.get(Client, camp.client_id) if camp.client_id else None
            client_name = client_obj.name if client_obj else camp.client_name
            client_company = client_obj.company_name if client_obj else client_name
            client_phone = client_obj.contact_number if client_obj else ""

            script = ""
            questions = []
            req_info = []
            trans_rules = "Qualify and transfer."
            if camp.schedule and isinstance(camp.schedule, dict):
                script = camp.schedule.get("script", "")
                questions = camp.schedule.get("qualification_questions", [])
                req_info = camp.schedule.get("required_info", [])
                trans_rules = camp.schedule.get("transfer_rules", "Qualify and transfer.")

            lead_full_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"

            assignments.append({
                "id": cl.id,
                "business_id": camp.business_id,
                "lead_id": lead.id,
                "expert_id": target_expert_id,
                "expert_name": exp_name,
                "expert_email": exp_email,
                "previous_expert_id": None,
                "previous_expert_name": None,
                "strategy": "CAMPAIGN_ASSIGNMENT",
                "status": "ACTIVE" if cl.status in ("PENDING", "CALLING", "IN_PROGRESS") else cl.status,
                "trigger_source": "CAMPAIGN",
                "ai_agent_name": camp.name,
                "qualification_stage": lead.qualification_status or "NEW",
                "transcript": "",
                "notes": "",
                "context": {
                    "name": lead_full_name,
                    "lead_name": lead_full_name,
                    "first_name": lead.first_name or "",
                    "last_name": lead.last_name or "",
                    "phone": lead.phone or "",
                    "email": lead.email or "",
                    "company": lead.company or "",
                    "location": lead.location or "",
                    "city": lead.location or "",
                    "requirement": lead.requirement or "Interested in campaign offering",
                    "lead_status": lead.status or "Ready",
                    "qualification_status": lead.qualification_status or "UNQUALIFIED",
                    "campaign_id": camp.id,
                    "campaign_name": camp.name,
                    "client_id": camp.client_id,
                    "client_name": client_name,
                    "client_company": client_company,
                    "client_phone": client_phone,
                    "lead_group_id": camp.lead_group_id,
                    "script": script,
                    "qualification_questions": questions,
                    "required_info": req_info,
                    "transfer_rules": trans_rules,
                },
                "assigned_at": cl.created_at,
            })

    # Include Leads from assigned campaign lead groups (in case CampaignLead table does not have entries yet)
    g_leads_stmt = (
        select(Lead, Campaign)
        .join(Campaign, Lead.group_id == Campaign.lead_group_id)
        .where(
            or_(
                Campaign.voice_agent_id.in_(agent_ids),
                Campaign.agent_id.in_(agent_ids),
                Campaign.business_id == business_id
            )
        )
    )
    if status_filter == "ACTIVE" or not status_filter:
        g_leads_stmt = g_leads_stmt.where(Lead.status.notin_(["COMPLETED", "DISQUALIFIED", "DNC"]))
    elif status_filter == "HISTORY":
        g_leads_stmt = g_leads_stmt.where(Lead.status.in_(["COMPLETED", "DISQUALIFIED", "DNC"]))

    g_rows = (await db.execute(g_leads_stmt)).all()
    for lead, camp in g_rows:
        if lead.id not in assigned_lead_ids:
            assigned_lead_ids.add(lead.id)
            client_obj = await db.get(Client, camp.client_id) if camp.client_id else None
            client_name = client_obj.name if client_obj else camp.client_name
            client_company = client_obj.company_name if client_obj else client_name
            client_phone = client_obj.contact_number if client_obj else ""

            script = ""
            questions = []
            req_info = []
            trans_rules = "Qualify and transfer."
            if camp.schedule and isinstance(camp.schedule, dict):
                script = camp.schedule.get("script", "")
                questions = camp.schedule.get("qualification_questions", [])
                req_info = camp.schedule.get("required_info", [])
                trans_rules = camp.schedule.get("transfer_rules", "Qualify and transfer.")

            lead_full_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"

            assignments.append({
                "id": f"group-assign-{lead.id}",
                "business_id": camp.business_id,
                "lead_id": lead.id,
                "expert_id": target_expert_id,
                "expert_name": exp_name,
                "expert_email": exp_email,
                "previous_expert_id": None,
                "previous_expert_name": None,
                "strategy": "CAMPAIGN_GROUP_ASSIGNMENT",
                "status": "ACTIVE",
                "trigger_source": "CAMPAIGN_GROUP",
                "ai_agent_name": camp.name,
                "qualification_stage": lead.qualification_status or "NEW",
                "transcript": "",
                "notes": "",
                "context": {
                    "name": lead_full_name,
                    "lead_name": lead_full_name,
                    "first_name": lead.first_name or "",
                    "last_name": lead.last_name or "",
                    "phone": lead.phone or "",
                    "email": lead.email or "",
                    "company": lead.company or "",
                    "location": lead.location or "",
                    "city": lead.location or "",
                    "requirement": lead.requirement or "Interested in campaign offering",
                    "lead_status": lead.status or "Ready",
                    "qualification_status": lead.qualification_status or "UNQUALIFIED",
                    "campaign_id": camp.id,
                    "campaign_name": camp.name,
                    "client_id": camp.client_id,
                    "client_name": client_name,
                    "client_company": client_company,
                    "client_phone": client_phone,
                    "lead_group_id": camp.lead_group_id,
                    "script": script,
                    "qualification_questions": questions,
                    "required_info": req_info,
                    "transfer_rules": trans_rules,
                },
                "assigned_at": lead.created_at,
            })

    # Include directly assigned Leads (Lead.assigned_expert_id in agent_ids)
    if agent_ids:
        direct_leads_stmt = (
            select(Lead)
            .where(
                and_(
                    Lead.assigned_expert_id.in_(agent_ids),
                    Lead.business_id == business_id
                )
            )
        )
        if status_filter == "ACTIVE" or not status_filter:
            direct_leads_stmt = direct_leads_stmt.where(Lead.status.notin_(["COMPLETED", "DISQUALIFIED", "DNC"]))
        elif status_filter == "HISTORY":
            direct_leads_stmt = direct_leads_stmt.where(Lead.status.in_(["COMPLETED", "DISQUALIFIED", "DNC"]))
        direct_rows = (await db.execute(direct_leads_stmt)).scalars().all()
        for lead in direct_rows:
            if lead.id not in assigned_lead_ids:
                assigned_lead_ids.add(lead.id)
                lead_full_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
                assignments.append({
                    "id": f"direct-assign-{lead.id}",
                    "business_id": lead.business_id,
                    "lead_id": lead.id,
                    "expert_id": target_expert_id or current_user.id,
                    "expert_name": exp_name,
                    "expert_email": exp_email,
                    "previous_expert_id": None,
                    "previous_expert_name": None,
                    "strategy": "DIRECT_ASSIGNMENT",
                    "status": "ACTIVE",
                    "trigger_source": "DIRECT_ASSIGNMENT",
                    "ai_agent_name": "Direct Lead",
                    "qualification_stage": lead.qualification_status or "NEW",
                    "transcript": "",
                    "notes": getattr(lead, "notes", None) or getattr(lead, "ai_summary", "") or "",
                    "context": {
                        "name": lead_full_name,
                        "lead_name": lead_full_name,
                        "first_name": lead.first_name or "",
                        "last_name": lead.last_name or "",
                        "phone": lead.phone or "",
                        "email": lead.email or "",
                        "company": lead.company or "",
                        "location": lead.location or "",
                        "city": lead.location or "",
                        "requirement": lead.requirement or "Assigned Lead",
                        "lead_status": lead.status or "Ready",
                        "qualification_status": lead.qualification_status or "UNQUALIFIED",
                    },
                    "assigned_at": lead.created_at,
                })

    return assignments


@router.get("/history/{lead_id}")
async def get_lead_assignment_history(
    lead_id: str,
    business_id: str = Depends(get_current_business_id),
    current_user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns complete assignment history, source metadata, and chronological timeline for a specific lead.
    """
    lead = await db.get(Lead, lead_id)
    if not lead:
        # Check if lead_id was passed as an assignment ID
        assign_check = await db.get(LeadAssignment, lead_id)
        if assign_check:
            lead = await db.get(Lead, assign_check.lead_id)
            
    if not lead:
        # Fallback check by phone or partial match
        l_res = await db.execute(select(Lead).where(or_(Lead.id == lead_id, Lead.phone == lead_id)))
        lead = l_res.scalars().first()

    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")

    # 1. Fetch all assignments for this lead
    assign_stmt = (
        select(LeadAssignment, Expert)
        .outerjoin(Expert, LeadAssignment.expert_id == Expert.id)
        .where(LeadAssignment.lead_id == lead.id)
        .order_by(LeadAssignment.assigned_at.asc())
    )
    assign_res = await db.execute(assign_stmt)
    assign_rows = assign_res.all()

    # 2. Fetch all calls for this lead
    call_stmt = select(Call).where(Call.lead_id == lead.id).order_by(Call.created_at.asc())
    call_res = await db.execute(call_stmt)
    call_rows = call_res.scalars().all()

    # 3. Fetch all transfers for this lead
    transfer_stmt = select(CallTransfer).where(CallTransfer.lead_id == lead.id).order_by(CallTransfer.created_at.asc())
    transfer_res = await db.execute(transfer_stmt)
    transfer_rows = transfer_res.scalars().all()

    # 4. Fetch queue requests
    queue_stmt = select(AgentAssignmentQueue).where(AgentAssignmentQueue.lead_id == lead.id).order_by(AgentAssignmentQueue.created_at.asc())
    queue_res = await db.execute(queue_stmt)
    queue_rows = queue_res.scalars().all()

    # 5. Fetch campaign association
    camp_stmt = (
        select(CampaignLead, Campaign)
        .join(Campaign, CampaignLead.campaign_id == Campaign.id)
        .where(CampaignLead.lead_id == lead.id)
    )
    camp_res = await db.execute(camp_stmt)
    camp_row = camp_res.first()

    # Build Customer Data
    full_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
    customer_data = {
        "lead_id": lead.id,
        "name": full_name,
        "first_name": lead.first_name or "",
        "last_name": lead.last_name or "",
        "phone": lead.phone or "—",
        "email": lead.email or "—",
        "company": lead.company or "—",
        "city": lead.location or "—",
        "requirement": lead.requirement or "Interested in campaign offering",
        "status": lead.status or "READY",
        "qualification_status": lead.qualification_status or "UNQUALIFIED",
        "qualification_reason": lead.ai_summary or (f"Associated with campaign {camp_row[1].name}" if camp_row else ""),
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }

    # Determine latest assignment info & sources
    latest_assign = assign_rows[-1][0] if assign_rows else None
    latest_expert = assign_rows[-1][1] if assign_rows else None

    current_agent_name = (
        latest_expert.name if latest_expert 
        else (current_user.full_name or "Voice Agent")
    )

    assigned_by = "Admin"
    assignment_source = "Admin Assignment"
    previous_agent_name = None
    transfer_reason = None
    assigned_on = lead.created_at

    if latest_assign:
        assigned_on = latest_assign.assigned_at
        if latest_assign.previous_expert_id:
            prev_exp = await db.get(Expert, latest_assign.previous_expert_id)
            previous_agent_name = prev_exp.name if prev_exp else "Previous Agent"
            assigned_by = previous_agent_name
            assignment_source = "Agent Transfer"
        elif latest_assign.trigger_source in ("SPECIFIC_AGENT_REQUEST", "AGENT_TRANSFER"):
            assignment_source = "Agent Transfer"
            assigned_by = (latest_assign.context or {}).get("previous_expert_name", "Previous Agent")
        elif latest_assign.trigger_source in ("TALK_TO_AGENT", "AI_QUALIFIED", "AI_AGENT") or latest_assign.ai_agent_name:
            assignment_source = "AI Agent Assignment"
            assigned_by = latest_assign.ai_agent_name or "AI Agent"
        elif latest_assign.trigger_source in ("CAMPAIGN", "CAMPAIGN_ASSIGNMENT", "MANUAL", "ADMIN"):
            assignment_source = "Admin Assignment"
            assigned_by = "Admin"
            
        if latest_assign.notes:
            transfer_reason = latest_assign.notes
            
    elif camp_row:
        assignment_source = "Admin Assignment"
        assigned_by = "Admin"
        assigned_on = camp_row[0].created_at

    assignment_info = {
        "current_agent": current_agent_name,
        "assigned_by": assigned_by,
        "assignment_source": assignment_source,
        "previous_agent": previous_agent_name,
        "assigned_on": assigned_on.isoformat() if assigned_on else None,
        "transfer_reason": transfer_reason,
        "status": lead.status or "READY",
        "qualification_status": lead.qualification_status or "UNQUALIFIED",
        "qualification_reason": lead.ai_summary or "",
    }

    # Call Summary
    last_call = call_rows[-1] if call_rows else None
    total_calls_count = len(call_rows)
    total_duration = sum((c.duration_seconds or 0) for c in call_rows)
    if total_duration == 0 and lead.call_duration:
        total_duration = lead.call_duration

    m = total_duration // 60
    s = total_duration % 60
    formatted_duration = f"{m:02d}:{s:02d}"

    call_summary = {
        "total_calls": total_calls_count if total_calls_count > 0 else (1 if lead.status == "COMPLETED" else 0),
        "last_call_at": (
            last_call.created_at.isoformat() if last_call and last_call.created_at 
            else (lead.updated_at.isoformat() if lead.status == "COMPLETED" and lead.updated_at else None)
        ),
        "last_call_status": (
            last_call.status if last_call 
            else (lead.status if lead.status in ("COMPLETED", "CALLING") else "NO_CALLS")
        ),
        "qualification": lead.qualification_status or (last_call.qualification_status if last_call else "UNQUALIFIED"),
        "call_duration": total_duration,
        "formatted_duration": formatted_duration,
        "recording_url": last_call.recording_url if last_call else None,
    }

    # Build Chronological Timeline
    timeline_events = []

    # 1. Lead Created / Initial Assignment
    initial_time = lead.created_at
    timeline_events.append({
        "id": f"evt-create-{lead.id}",
        "timestamp": initial_time.isoformat() if initial_time else None,
        "time_raw": initial_time,
        "title": f"Assigned to {current_agent_name}",
        "type": "ASSIGNMENT",
        "actor": assigned_by,
        "assigned_to": current_agent_name,
        "assigned_by": assigned_by,
        "source": assignment_source,
        "status": "READY",
        "description": f"Customer lead assigned via {assignment_source} by {assigned_by}.",
    })

    # 2. Queue Events
    for q in queue_rows:
        if q.created_at:
            timeline_events.append({
                "id": f"evt-queue-{q.id}",
                "timestamp": q.created_at.isoformat(),
                "time_raw": q.created_at,
                "title": "Placed in Voice Queue",
                "type": "QUEUE",
                "actor": q.trigger_source or "System",
                "status": q.status,
                "description": f"Lead queued for available agent. Reason: {q.reason}",
            })

    # 3. Reassignment / Transfer Events
    for assign, expert in assign_rows:
        if assign.previous_expert_id:
            prev_exp = await db.get(Expert, assign.previous_expert_id)
            prev_name = prev_exp.name if prev_exp else "Previous Agent"
            curr_name = expert.name if expert else current_agent_name
            timeline_events.append({
                "id": f"evt-assign-{assign.id}",
                "timestamp": assign.assigned_at.isoformat() if assign.assigned_at else None,
                "time_raw": assign.assigned_at,
                "title": f"Transferred to {curr_name}",
                "type": "TRANSFER",
                "actor": prev_name,
                "assigned_to": curr_name,
                "assigned_by": prev_name,
                "source": "Agent Transfer",
                "status": assign.status,
                "description": f"Transferred from {prev_name} to {curr_name}. {assign.notes or ''}",
            })

    for tr in transfer_rows:
        from_user = await db.get(User, tr.from_agent_id) if tr.from_agent_id else None
        to_user = await db.get(User, tr.to_agent_id) if tr.to_agent_id else None
        from_name = from_user.full_name if from_user else "Agent"
        to_name = to_user.full_name if to_user else "Agent"
        timeline_events.append({
            "id": f"evt-transfer-{tr.id}",
            "timestamp": tr.created_at.isoformat() if tr.created_at else None,
            "time_raw": tr.created_at,
            "title": f"Call Transferred: {from_name} → {to_name}",
            "type": "TRANSFER",
            "actor": from_name,
            "status": tr.status,
            "description": f"Reason: {tr.reason or 'Agent Consultation / Handoff' if hasattr(tr, 'reason') else 'Agent Consultation / Handoff'}",
        })

    # 4. Calls
    if call_rows:
        for c in call_rows:
            if c.created_at:
                timeline_events.append({
                    "id": f"evt-call-start-{c.id}",
                    "timestamp": c.created_at.isoformat(),
                    "time_raw": c.created_at,
                    "title": "Call Started",
                    "type": "CALL_STARTED",
                    "actor": current_agent_name,
                    "status": "CALLING",
                    "description": f"Voice Agent {current_agent_name} initiated outbound call to customer.",
                })
                
                c_dur = c.duration_seconds or 0
                c_m = c_dur // 60
                c_s = c_dur % 60
                c_fmt = f"{c_m:02d}:{c_s:02d}"
                timeline_events.append({
                    "id": f"evt-call-end-{c.id}",
                    "timestamp": c.created_at.isoformat(),
                    "time_raw": c.created_at,
                    "title": "Call Completed",
                    "type": "CALL_COMPLETED",
                    "actor": current_agent_name,
                    "status": c.status or "COMPLETED",
                    "qualification": c.qualification_status or lead.qualification_status or "QUALIFIED",
                    "duration": c_fmt,
                    "description": f"Call ended with status {c.status or 'COMPLETED'} (Duration: {c_fmt}). Qualification: {c.qualification_status or lead.qualification_status or 'UNQUALIFIED'}.",
                })
    elif lead.status == "COMPLETED" or lead.call_status == "COMPLETED":
        up_time = lead.updated_at or lead.created_at
        timeline_events.append({
            "id": f"evt-call-completed-lead-{lead.id}",
            "timestamp": up_time.isoformat() if up_time else None,
            "time_raw": up_time,
            "title": "Call Completed",
            "type": "CALL_COMPLETED",
            "actor": current_agent_name,
            "status": "COMPLETED",
            "qualification": lead.qualification_status or "QUALIFIED",
            "duration": formatted_duration,
            "description": f"Call completed by {current_agent_name}. Status: COMPLETED. Qualification: {lead.qualification_status or 'UNQUALIFIED'}.",
        })

    # Sort timeline chronologically
    timeline_events.sort(key=lambda x: x.get("time_raw") or datetime.min)
    for evt in timeline_events:
        evt.pop("time_raw", None)

    return {
        "customer": customer_data,
        "assignment": assignment_info,
        "call_summary": call_summary,
        "timeline": timeline_events,
    }


