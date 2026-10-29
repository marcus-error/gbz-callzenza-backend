import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import AgentAssignmentQueue, CallTransfer, CampaignLead, Expert, Lead, LeadAssignment, User


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _edit_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _edit_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    previous_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        current_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = previous_row[j + 1] + 1
            deletions = current_row[j] + 1
            substitutions = previous_row[j] + (c1 != c2)
            current_row.append(min(insertions, deletions, substitutions))
        previous_row = current_row
    return previous_row[-1]


async def _broadcast_lead_assignment(
    db: AsyncSession,
    business_id: str,
    lead: Lead,
    expert: Expert,
    assignment_id: str,
    call_id: Optional[str] = None,
    campaign_id: Optional[str] = None,
    ai_agent_name: str = "Voice Agent AI",
    qualification_stage: str = "QUALIFIED",
    trigger_source: str = "TALK_TO_AGENT",
    notes: str = "",
    context_data: Optional[Dict[str, Any]] = None,
):
    try:
        from app.api.routes.websockets import global_ws_manager
        from app.db.models import Call, Campaign, Client, CallTransfer

        lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Client Lead"
        lead_dict = {
            "id": lead.id,
            "first_name": lead.first_name or "",
            "last_name": lead.last_name or "",
            "name": lead_name,
            "phone": lead.phone or "",
            "email": lead.email or "",
            "company": lead.company or "",
            "job_title": getattr(lead, "job_title", "") or "",
            "requirement": getattr(lead, "requirement", "") or notes,
            "location": getattr(lead, "location", "") or "",
            "status": lead.status or "ASSIGNED",
            "qualification_status": lead.qualification_status or qualification_stage,
            "campaign_id": campaign_id or "",
            "notes": notes or "",
        }

        # Resolve Campaign Client info if available
        campaign_client_name = ""
        campaign_client_phone = ""
        if campaign_id:
            camp = await db.get(Campaign, campaign_id)
            if camp:
                stmt_c = select(Client).where(Client.business_id == (camp.business_id or business_id))
                cl = (await db.execute(stmt_c)).scalars().first()
                if cl:
                    campaign_client_name = cl.name or cl.company_name or ""
                    campaign_client_phone = cl.contact_number or ""

        effective_call_id = call_id or f"call_{lead.id[:8]}"

        # Ensure Call record exists so the voice agent can immediately handle and connect client
        call_obj = await db.get(Call, effective_call_id) if effective_call_id else None
        if not call_obj and effective_call_id:
            call_obj = Call(
                id=effective_call_id,
                business_id=business_id or "default",
                lead_id=lead.id,
                agent_id=expert.user_id or expert.id,
                status="TRANSFER_REQUESTED",
                direction="INBOUND",
                agent_type="HUMAN",
                client_name=lead_name,
                client_phone=lead.phone or "+1-555-0199",
            )
            db.add(call_obj)
            await db.commit()
        elif call_obj:
            call_obj.status = "TRANSFER_REQUESTED"
            call_obj.agent_type = "HUMAN"
            call_obj.lead_id = lead.id
            if expert.user_id:
                call_obj.agent_id = expert.user_id
            db.add(call_obj)
            await db.commit()

        # Ensure CallTransfer record exists for this transfer request
        stmt_tr = (
            select(CallTransfer)
            .where(
                or_(
                    CallTransfer.call_id == effective_call_id,
                    CallTransfer.lead_id == lead.id
                )
            )
            .order_by(CallTransfer.created_at.desc())
        )
        transfer_row = (await db.execute(stmt_tr)).scalars().first()
        if not transfer_row:
            active_transfer_id = str(uuid.uuid4())
            transfer_row = CallTransfer(
                id=active_transfer_id,
                business_id=business_id or "default",
                call_id=effective_call_id,
                lead_id=lead.id,
                from_agent_id=ai_agent_name or "Voice Agent AI",
                to_agent_id=expert.user_id or expert.id,
                transfer_type="warm",
                status="PENDING",
                created_at=_now(),
                updated_at=_now(),
            )
            db.add(transfer_row)
            await db.commit()
        else:
            active_transfer_id = transfer_row.id

        # 1. Real-time Lead Assigned event
        assigned_event = {
            "event": "LEAD_ASSIGNED",
            "type": "LEAD_ASSIGNED",
            "lead_id": lead.id,
            "assignment_id": assignment_id,
            "call_id": effective_call_id,
            "call_session_id": effective_call_id,
            "client_id": lead.id,
            "client_name": lead_name,
            "client_phone": lead.phone or "",
            "lead": lead_dict,
            "context": context_data or {},
            "from_agent_name": ai_agent_name,
            "to_agent_id": expert.user_id or expert.id,
            "to_agent_name": expert.name,
            "to_agent_email": expert.email,
            "qualification_stage": qualification_stage,
            "trigger_source": trigger_source,
            "campaign_id": campaign_id or "",
            "campaign_client_name": campaign_client_name,
            "campaign_client_phone": campaign_client_phone,
            "notes": notes,
            "timestamp": _now().isoformat(),
        }

        # 2. Real-time Transfer Requested event (modal popup for voice agent)
        transfer_event = {
            "event": "TRANSFER_REQUESTED",
            "type": "TRANSFER_REQUESTED",
            "transfer_id": active_transfer_id,
            "call_id": effective_call_id,
            "call_session_id": effective_call_id,
            "lead_id": lead.id,
            "client_id": lead.id,
            "client_name": lead_name,
            "client_phone": lead.phone or "",
            "from_agent_name": ai_agent_name or "Voice Agent AI",
            "from_voice_agent_name": ai_agent_name or "Voice Agent AI",
            "to_agent_id": expert.user_id or expert.id,
            "to_voice_agent_id": expert.user_id or expert.id,
            "target_user_id": expert.user_id,
            "to_expert_id": expert.id,
            "target_expert_id": expert.id,
            "to_agent_name": expert.name,
            "to_agent_email": expert.email,
            "lead": lead_dict,
            "lead_name": lead_name,
            "lead_phone": lead.phone or "",
            "lead_email": lead.email or "",
            "lead_company": lead.company or "",
            "lead_location": getattr(lead, "location", "") or "",
            "lead_requirement": getattr(lead, "requirement", "") or notes,
            "campaign_id": campaign_id or "",
            "campaign_client_name": campaign_client_name,
            "campaign_client_phone": campaign_client_phone,
            "notes": notes,
            "timestamp": _now().isoformat(),
        }

        # Target destinations: expert.user_id, expert.id, expert.email
        targets = set(filter(None, [expert.user_id, expert.id, expert.email]))
        for tgt in targets:
            await global_ws_manager.send_to_user(tgt, assigned_event)
            await global_ws_manager.send_to_user(tgt, transfer_event)

        await global_ws_manager.broadcast_to_business(business_id or "default", assigned_event)
        await global_ws_manager.broadcast_to_business(business_id or "default", transfer_event)
        await global_ws_manager.broadcast(assigned_event)
        await global_ws_manager.broadcast(transfer_event)
        logger.info(f"[WS BROADCAST] Pushed lead {lead.id} transfer events to expert {expert.name} ({targets})")

        # Non-blocking notification email to assigned expert
        if expert.email and "@" in expert.email:
            try:
                from app.services.email_service import email_service
                subject = f"🎯 New Lead Assigned: {lead_name}"
                html = f"""
                <div style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto; padding: 20px; border: 1px solid #e5e7eb; border-radius: 8px;">
                    <h2 style="color: #4f46e5; margin-top: 0;">New Lead Assignment</h2>
                    <p>Hello <strong>{expert.name}</strong>,</p>
                    <p>A new lead has been assigned to you by <strong>{ai_agent_name}</strong>.</p>
                    <table style="width: 100%; border-collapse: collapse; margin: 20px 0;">
                        <tr><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #6b7280;">Lead Name</td><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; font-weight: bold;">{lead_name}</td></tr>
                        <tr><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #6b7280;">Phone</td><td style="padding: 8px; border-bottom: 1px solid #f3f4f6;">{lead.phone or 'N/A'}</td></tr>
                        <tr><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #6b7280;">Email</td><td style="padding: 8px; border-bottom: 1px solid #f3f4f6;">{lead.email or 'N/A'}</td></tr>
                        <tr><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #6b7280;">Qualification</td><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #16a34a; font-weight: bold;">{qualification_stage}</td></tr>
                        <tr><td style="padding: 8px; border-bottom: 1px solid #f3f4f6; color: #6b7280;">Notes / Summary</td><td style="padding: 8px; border-bottom: 1px solid #f3f4f6;">{notes or 'None provided'}</td></tr>
                    </table>
                    <p style="color: #9ca3af; font-size: 12px; margin-top: 24px;">Generated automatically by CallZenza AI Platform.</p>
                </div>
                """
                await email_service.send_email(
                    to_email=expert.email,
                    subject=subject,
                    html_content=html,
                    business_id=business_id,
                    db=db,
                    metadata={"lead_id": lead.id, "expert_id": expert.id, "type": "lead_assignment"}
                )
                logger.info(f"[EMAIL] Lead assignment notification sent to {expert.email}")
            except Exception as mail_err:
                logger.warning(f"[EMAIL WARNING] Failed to send lead assignment email to {expert.email}: {mail_err}")
    except Exception as ws_err:
        logger.warning(f"[WS BROADCAST WARNING] Could not broadcast transfer events: {ws_err}")


class LeadAssignmentService:

    @staticmethod
    async def find_expert_by_name(db: AsyncSession, name: str, business_id: Optional[str] = None) -> Optional[Expert]:
        """
        Fuzzy matches a Voice Agent by name against Expert records.
        Handles exact, substring, first-name, and edit distance matches (e.g. "aroura" -> "Aurora", "asu" -> "Asu").
        """
        if not name or not name.strip():
            return None

        clean_input = name.strip().lower()

        # Query experts
        query = select(Expert)
        if business_id:
            query = query.where(Expert.business_id == business_id)
        res = await db.execute(query)
        experts = res.scalars().all()
        if not experts:
            # Fallback check all experts
            res_all = await db.execute(select(Expert))
            experts = res_all.scalars().all()

        if not experts:
            return None

        best_match = None
        min_dist = 999

        for exp in experts:
            exp_name_clean = exp.name.strip().lower()
            first_name = exp_name_clean.split()[0] if exp_name_clean else ""

            # Exact or substring match
            if clean_input == exp_name_clean or clean_input == first_name:
                return exp
            if clean_input in exp_name_clean or exp_name_clean in clean_input:
                return exp

            # Edit distance check for phonetic typos like "aroura" vs "aurora"
            dist = _edit_distance(clean_input, exp_name_clean)
            dist_first = _edit_distance(clean_input, first_name) if first_name else 999
            effective_dist = min(dist, dist_first)

            if effective_dist < min_dist and effective_dist <= 2:
                min_dist = effective_dist
                best_match = exp

        return best_match

    @staticmethod
    async def transfer_lead_to_agent(
        db: AsyncSession,
        business_id: str,
        lead_id: str,
        call_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        ai_agent_name: str = "Voice Agent AI",
        qualification_stage: str = "QUALIFIED",
        trigger_source: str = "TALK_TO_AGENT",
        transcript: str = "",
        notes: str = "",
        collected_answers: Optional[Dict[str, Any]] = None,
        requested_expert_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Centralized Lead Assignment Engine:
        Handles general AI transfer requests, completed qualification events, and specific agent requests.
        """
        # Fetch lead with row lock
        lead_query = select(Lead).where(Lead.id == lead_id, Lead.business_id == business_id).with_for_update()
        lead_res = await db.execute(lead_query)
        lead = lead_res.scalars().first()

        if not lead:
            if lead_id and len(lead_id) == 36:
                lead = await db.get(Lead, lead_id)
            if not lead:
                phone = (collected_answers and collected_answers.get("phone")) or ""
                email = (collected_answers and collected_answers.get("email")) or ""
                if phone or email:
                    stmt = select(Lead).where(or_(Lead.phone == phone, Lead.email == email))
            if not lead and campaign_id:
                stmt_cl = select(Lead).join(CampaignLead, CampaignLead.lead_id == Lead.id).where(CampaignLead.campaign_id == campaign_id)
                lead = (await db.execute(stmt_cl)).scalars().first()

            if not lead:
                cust_name = (collected_answers and collected_answers.get("customer_name")) or "Customer"
                cust_phone = (collected_answers and collected_answers.get("phone")) or "+1-555-0199"
                lead = Lead(
                    id=lead_id if (lead_id and len(lead_id) == 36) else str(uuid.uuid4()),
                    business_id=business_id or "default",
                    first_name=cust_name.split()[0] if cust_name else "Customer",
                    last_name=" ".join(cust_name.split()[1:]) if len(cust_name.split()) > 1 else "",
                    phone=cust_phone,
                    status="ASSIGNED",
                    qualification_status=qualification_stage or "QUALIFIED",
                    created_at=_now(),
                )
                db.add(lead)
                await db.flush()

        if trigger_source == "QUALIFIED_LEAD" or qualification_stage == "QUALIFIED":
            lead.qualification_status = "QUALIFIED"

        # Check existing active assignment and previous expert
        old_expert_id = lead.assigned_expert_id
        old_expert = None
        if old_expert_id:
            old_expert = await db.get(Expert, old_expert_id)

        # ---------------------------------------------------------------------
        # SPECIFIC AGENT REQUEST FLOW
        # ---------------------------------------------------------------------
        if requested_expert_name:
            trigger_source = "SPECIFIC_AGENT_REQUEST"
            requested_expert = await LeadAssignmentService.find_expert_by_name(db, requested_expert_name, business_id)

            if not requested_expert:
                logger.warning(f"[ASSIGNMENT] Requested agent '{requested_expert_name}' not found in DB.")
                return {
                    "success": False,
                    "status": "NOT_FOUND",
                    "message": f"Requested voice agent '{requested_expert_name}' was not found.",
                    "requested_expert_name": requested_expert_name,
                    "lead_id": lead.id,
                }

            # Check if already assigned to requested agent
            if old_expert_id == requested_expert.id:
                logger.info(f"[ASSIGNMENT] Lead {lead.id} is ALREADY assigned to requested expert {requested_expert.name}")
                return {
                    "success": True,
                    "status": "ALREADY_ASSIGNED",
                    "message": f"Lead is already assigned to {requested_expert.name}",
                    "lead_id": lead.id,
                    "assigned_expert": {
                        "id": requested_expert.id,
                        "name": requested_expert.name,
                        "email": requested_expert.email,
                        "status": requested_expert.status,
                    },
                }

            # Close previous assignment if moving from another agent (e.g. Aurora -> Asu)
            if old_expert and old_expert.id != requested_expert.id:
                logger.info(f"[REASSIGNMENT] Closing active assignment for previous expert {old_expert.name} ({old_expert.id})")
                old_assign_res = await db.execute(
                    select(LeadAssignment).where(
                        LeadAssignment.lead_id == lead.id,
                        LeadAssignment.expert_id == old_expert.id,
                        LeadAssignment.status == "ACTIVE"
                    )
                )
                for old_assign in old_assign_res.scalars().all():
                    old_assign.status = "REASSIGNED"
                    old_assign.notes = f"Reassigned to requested agent {requested_expert.name} on {datetime.now().strftime('%Y-%m-%d %H:%M')}"

                old_expert.active_lead_count = max(0, (old_expert.active_lead_count or 1) - 1)

            # Check requested agent availability
            if requested_expert.status in ["AVAILABLE", "FREE"]:
                lead.assigned_expert_id = requested_expert.id
                lead.status = "ASSIGNED"
                requested_expert.status = "BUSY"
                requested_expert.active_lead_count = (requested_expert.active_lead_count or 0) + 1

                context_data = {
                    "lead_name": f"{lead.first_name} {lead.last_name}".strip() or lead.email,
                    "phone": lead.phone,
                    "email": lead.email,
                    "company": lead.company,
                    "campaign_id": campaign_id,
                    "call_id": call_id,
                    "collected_answers": collected_answers or {},
                    "transfer_time": _now().isoformat(),
                    "trigger_source": "SPECIFIC_AGENT_REQUEST",
                    "requested_agent_name": requested_expert.name,
                    "previous_expert_name": old_expert.name if old_expert else None,
                    "previous_expert_id": old_expert.id if old_expert else None,
                }

                new_assignment = LeadAssignment(
                    id=str(uuid.uuid4()),
                    business_id=business_id,
                    lead_id=lead.id,
                    expert_id=requested_expert.id,
                    previous_expert_id=old_expert.id if old_expert else None,
                    strategy="SPECIFIC_AGENT_REQUEST",
                    status="ACTIVE",
                    trigger_source="SPECIFIC_AGENT_REQUEST",
                    ai_agent_name=ai_agent_name,
                    qualification_stage=qualification_stage,
                    transcript=transcript,
                    notes=notes or f"Specific agent request for {requested_expert.name}",
                    context=context_data,
                    assigned_at=_now(),
                )
                db.add(new_assignment)

                # Clear queue items
                q_res = await db.execute(
                    select(AgentAssignmentQueue).where(
                        AgentAssignmentQueue.lead_id == lead.id,
                        AgentAssignmentQueue.status == "WAITING"
                    )
                )
                for q_i in q_res.scalars().all():
                    q_i.status = "ASSIGNED"
                    q_i.assigned_at = _now()

                # Update CampaignLead assigned expert ONLY for this specific transferred customer lead
                stmt_cl = select(CampaignLead).where(CampaignLead.lead_id == lead.id)
                c_leads = (await db.execute(stmt_cl)).scalars().all()
                for cl in c_leads:
                    cl.assigned_expert_id = requested_expert.id
                    db.add(cl)

                await db.commit()
                logger.info(f"[ASSIGNMENT] Reassigned Lead {lead.id} to requested agent {requested_expert.name}")

                await _broadcast_lead_assignment(
                    db=db,
                    business_id=business_id,
                    lead=lead,
                    expert=requested_expert,
                    assignment_id=new_assignment.id,
                    call_id=call_id,
                    campaign_id=campaign_id,
                    ai_agent_name=ai_agent_name,
                    qualification_stage=qualification_stage,
                    trigger_source=trigger_source,
                    notes=notes or f"Specific agent request for {requested_expert.name}",
                    context_data=context_data,
                )

                return {
                    "success": True,
                    "status": "ASSIGNED",
                    "message": f"Successfully assigned lead to requested agent {requested_expert.name}",
                    "lead_id": lead.id,
                    "assignment_id": new_assignment.id,
                    "assigned_expert": {
                        "id": requested_expert.id,
                        "name": requested_expert.name,
                        "email": requested_expert.email,
                        "status": requested_expert.status,
                    },
                    "previous_expert": {
                        "id": old_expert.id,
                        "name": old_expert.name,
                    } if old_expert else None,
                }
            else:
                # Requested agent is BUSY / UNAVAILABLE
                lead.assigned_expert_id = None
                lead.status = "WAITING_FOR_REQUESTED_AGENT"

                # Check existing queue item
                q_check = await db.execute(
                    select(AgentAssignmentQueue).where(
                        AgentAssignmentQueue.lead_id == lead.id,
                        AgentAssignmentQueue.status == "WAITING"
                    )
                )
                existing_q = q_check.scalars().first()
                q_item_id = None
                if not existing_q:
                    q_item = AgentAssignmentQueue(
                        id=str(uuid.uuid4()),
                        business_id=business_id,
                        lead_id=lead.id,
                        call_id=call_id,
                        campaign_id=campaign_id,
                        requested_expert_id=requested_expert.id,
                        trigger_source="SPECIFIC_AGENT_REQUEST",
                        reason=f"REQUESTED_AGENT_BUSY ({requested_expert.name})",
                        status="WAITING",
                        created_at=_now(),
                    )
                    db.add(q_item)
                    await db.flush()
                    q_item_id = q_item.id
                else:
                    existing_q.requested_expert_id = requested_expert.id
                    existing_q.reason = f"REQUESTED_AGENT_BUSY ({requested_expert.name})"
                    q_item_id = existing_q.id

                await db.commit()
                logger.warning(f"[ASSIGNMENT QUEUE] Requested agent {requested_expert.name} is BUSY. Placed Lead {lead.id} in personal queue.")

                return {
                    "success": True,
                    "status": "QUEUED_FOR_REQUESTED_AGENT",
                    "message": f"{requested_expert.name} is currently assisting another customer. Placed in {requested_expert.name}'s queue.",
                    "lead_id": lead.id,
                    "queue_item_id": q_item_id,
                    "requested_expert": {
                        "id": requested_expert.id,
                        "name": requested_expert.name,
                        "status": requested_expert.status,
                    },
                    "previous_expert": {
                        "id": old_expert.id,
                        "name": old_expert.name,
                    } if old_expert else None,
                }

        # ---------------------------------------------------------------------
        # GENERAL AGENT ASSIGNMENT FLOW (NO SPECIFIC AGENT REQUESTED)
        # ---------------------------------------------------------------------
        expert = None
        if lead.assigned_expert_id:
            expert = await db.get(Expert, lead.assigned_expert_id)
            if expert:
                logger.info(f"[ASSIGNMENT] Lead {lead_id} is already assigned to Expert {expert.name} ({expert.id}) - broadcasting live transfer.")
                context_data = {
                    "lead_name": f"{lead.first_name} {lead.last_name}".strip() or lead.email,
                    "phone": lead.phone,
                    "email": lead.email,
                    "company": lead.company,
                    "campaign_id": campaign_id,
                    "call_id": call_id,
                    "collected_answers": collected_answers or {},
                    "transfer_time": _now().isoformat(),
                    "trigger_source": trigger_source,
                    "follow_up_status": "PENDING_CONTACT",
                }
                await _broadcast_lead_assignment(
                    db=db,
                    business_id=business_id,
                    lead=lead,
                    expert=expert,
                    assignment_id=str(uuid.uuid4()),
                    call_id=call_id,
                    campaign_id=campaign_id,
                    ai_agent_name=ai_agent_name,
                    qualification_stage=qualification_stage,
                    trigger_source=trigger_source,
                    notes=notes or f"Transferred call to assigned agent {expert.name}",
                    context_data=context_data,
                )
                return {
                    "success": True,
                    "status": "ALREADY_ASSIGNED",
                    "message": f"Lead is already assigned to {expert.name}. Live transfer routed to agent.",
                    "lead_id": lead.id,
                    "assigned_expert": {
                        "id": expert.id,
                        "name": expert.name,
                        "email": expert.email,
                        "status": expert.status,
                    },
                }

        if not expert and campaign_id:
            from app.db.models import Campaign
            camp = await db.get(Campaign, campaign_id)
            if camp and camp.voice_agent_id:
                expert = await db.get(Expert, camp.voice_agent_id)

        if not expert:
            query_expert = (
                select(Expert)
                .outerjoin(User, Expert.user_id == User.id)
                .where(
                    Expert.business_id == business_id,
                    Expert.status.in_(["AVAILABLE", "FREE"]),
                    (User.role == "VOICE_AGENT") | (User.role == "ADMIN") | (User.role == "SUPER_ADMIN") | (Expert.user_id == None)
                )
                .order_by(Expert.active_lead_count.asc(), Expert.created_at.asc())
                .with_for_update(of=Expert)
            )
            expert_res = await db.execute(query_expert)
            expert = expert_res.scalars().first()

        if not expert:
            query_all = (
                select(Expert)
                .where(Expert.status.in_(["AVAILABLE", "FREE"]))
                .order_by(Expert.active_lead_count.asc(), Expert.created_at.asc())
                .with_for_update(of=Expert)
            )
            expert = (await db.execute(query_all)).scalars().first()

        if not expert:
            query_any = select(Expert).order_by(Expert.created_at.asc()).with_for_update(of=Expert)
            expert = (await db.execute(query_any)).scalars().first()

        if expert:
            lead.assigned_expert_id = expert.id
            lead.status = "ASSIGNED"
            expert.status = "BUSY"
            expert.active_lead_count = (expert.active_lead_count or 0) + 1

            context_data = {
                "lead_name": f"{lead.first_name} {lead.last_name}".strip() or lead.email,
                "phone": lead.phone,
                "email": lead.email,
                "company": lead.company,
                "campaign_id": campaign_id,
                "call_id": call_id,
                "collected_answers": collected_answers or {},
                "transfer_time": _now().isoformat(),
                "trigger_source": trigger_source,
                "follow_up_status": "PENDING_CONTACT",
            }

            assignment = LeadAssignment(
                id=str(uuid.uuid4()),
                business_id=business_id,
                lead_id=lead.id,
                expert_id=expert.id,
                strategy=trigger_source,
                status="ACTIVE",
                trigger_source=trigger_source,
                ai_agent_name=ai_agent_name,
                qualification_stage=qualification_stage,
                transcript=transcript,
                notes=notes,
                context=context_data,
                assigned_at=_now(),
            )
            db.add(assignment)

            queue_res = await db.execute(
                select(AgentAssignmentQueue).where(
                    AgentAssignmentQueue.lead_id == lead.id,
                    AgentAssignmentQueue.status == "WAITING"
                )
            )
            for q_item in queue_res.scalars().all():
                q_item.status = "ASSIGNED"
                q_item.assigned_at = _now()

            # Update CampaignLead assigned expert ONLY for this specific transferred customer lead
            stmt_cl = select(CampaignLead).where(CampaignLead.lead_id == lead.id)
            c_leads = (await db.execute(stmt_cl)).scalars().all()
            for cl in c_leads:
                cl.assigned_expert_id = expert.id
                db.add(cl)

            await db.commit()
            logger.info(f"[ASSIGNMENT] Successfully assigned Lead {lead.id} ({trigger_source}) to Expert {expert.name}")

            await _broadcast_lead_assignment(
                db=db,
                business_id=business_id,
                lead=lead,
                expert=expert,
                assignment_id=assignment.id,
                call_id=call_id,
                campaign_id=campaign_id,
                ai_agent_name=ai_agent_name,
                qualification_stage=qualification_stage,
                trigger_source=trigger_source,
                notes=notes,
                context_data=context_data,
            )

            return {
                "success": True,
                "status": "ASSIGNED",
                "message": f"Successfully assigned lead to agent {expert.name} via {trigger_source}",
                "lead_id": lead.id,
                "assignment_id": assignment.id,
                "assigned_expert": {
                    "id": expert.id,
                    "name": expert.name,
                    "email": expert.email,
                    "status": expert.status,
                },
            }

        lead.status = "WAITING_FOR_AGENT"
        q_check = await db.execute(
            select(AgentAssignmentQueue).where(
                AgentAssignmentQueue.lead_id == lead.id,
                AgentAssignmentQueue.status == "WAITING"
            )
        )
        existing_queue = q_check.scalars().first()

        queue_item_id = None
        if not existing_queue:
            q_item = AgentAssignmentQueue(
                id=str(uuid.uuid4()),
                business_id=business_id,
                lead_id=lead.id,
                call_id=call_id,
                campaign_id=campaign_id,
                trigger_source=trigger_source,
                reason="NO_AGENT_AVAILABLE",
                status="WAITING",
                created_at=_now(),
            )
            db.add(q_item)
            await db.flush()
            queue_item_id = q_item.id
        else:
            queue_item_id = existing_queue.id

        await db.commit()
        logger.warning(f"[ASSIGNMENT QUEUE] No agent free for Lead {lead.id} ({trigger_source}). Placed in AgentAssignmentQueue (ID: {queue_item_id})")

        return {
            "success": True,
            "status": "QUEUED",
            "message": f"No human agent currently free. Lead placed into assignment queue ({trigger_source}).",
            "lead_id": lead.id,
            "queue_item_id": queue_item_id,
            "assigned_expert": None,
        }

    @staticmethod
    async def complete_ai_call_assignment(
        db: AsyncSession,
        business_id: str,
        lead_id: str,
        call_id: Optional[str] = None,
        qualification_status: str = "QUALIFIED",
        summary: str = "",
        transcript: str = "",
        notes: str = "",
        collected_answers: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executed upon AI call completion / qualification:
        If lead is not assigned, automatically assigns to a free agent or queues the lead using trigger_source="QUALIFIED_LEAD".
        """
        return await LeadAssignmentService.transfer_lead_to_agent(
            db=db,
            business_id=business_id,
            lead_id=lead_id,
            call_id=call_id,
            ai_agent_name="AI Voice Call System",
            qualification_stage=qualification_status,
            trigger_source="QUALIFIED_LEAD",
            transcript=transcript,
            notes=f"AI Call Completed ({qualification_status}): {summary}\n{notes}".strip(),
            collected_answers=collected_answers,
        )

    @staticmethod
    async def process_assignment_queue(db: AsyncSession, business_id: Optional[str] = None, expert_id: Optional[str] = None) -> int:
        """
        Checks for waiting queue items and assigns them to available human agents.
        Prioritizes requested-agent queue items when a specific expert becomes available!
        Returns the number of successfully assigned queue items.
        """
        assigned_count = 0

        # 1. If a specific expert became available, check if leads are waiting in that expert's personal queue FIRST
        if expert_id:
            expert = await db.get(Expert, expert_id)
            if expert and expert.status in ["AVAILABLE", "FREE"]:
                q_req = await db.execute(
                    select(AgentAssignmentQueue)
                    .where(
                        AgentAssignmentQueue.requested_expert_id == expert_id,
                        AgentAssignmentQueue.status == "WAITING"
                    )
                    .order_by(AgentAssignmentQueue.created_at.asc())
                    .with_for_update()
                )
                req_items = q_req.scalars().all()
                for q_i in req_items:
                    stmt_lead = select(Lead).where(Lead.id == q_i.lead_id).with_for_update()
                    lead = (await db.execute(stmt_lead)).scalars().first()
                    if not lead:
                        q_i.status = "CANCELLED"
                        continue

                    if lead.status == "ASSIGNED" and lead.assigned_expert_id:
                        logger.info(f"[RACE CONDITION PREVENTED] Lead {lead.id} is already assigned to {lead.assigned_expert_id}.")
                        q_i.status = "ASSIGNED"
                        q_i.assigned_at = _now()
                        continue

                    # Assign lead to requested expert
                    lead.assigned_expert_id = expert.id
                    lead.status = "ASSIGNED"
                    expert.status = "BUSY"
                    expert.active_lead_count = (expert.active_lead_count or 0) + 1
                    q_i.status = "ASSIGNED"
                    q_i.assigned_at = _now()

                    assignment = LeadAssignment(
                        id=str(uuid.uuid4()),
                        business_id=q_i.business_id,
                        lead_id=lead.id,
                        expert_id=expert.id,
                        strategy="REQUESTED_QUEUE_AUTO_ASSIGN",
                        status="ACTIVE",
                        trigger_source="SPECIFIC_AGENT_REQUEST",
                        ai_agent_name="Requested Queue Processor",
                        qualification_stage=lead.qualification_status or "QUALIFIED",
                        notes=f"Assigned automatically to requested agent {expert.name} upon availability.",
                        context={"queue_item_id": q_i.id, "requested_expert_id": expert.id},
                        assigned_at=_now(),
                    )
                    db.add(assignment)
                    assigned_count += 1
                    logger.info(f"[REQUESTED QUEUE PROCESSOR] Assigned Queued Lead {lead.id} to requested Expert {expert.name}")
                    break  # Expert is now BUSY

        # 2. Process general waiting queue items
        query_queue = (
            select(AgentAssignmentQueue)
            .where(AgentAssignmentQueue.status == "WAITING")
            .order_by(AgentAssignmentQueue.created_at.asc())
            .with_for_update()
        )
        if business_id:
            query_queue = query_queue.where(AgentAssignmentQueue.business_id == business_id)

        queue_res = await db.execute(query_queue)
        waiting_items = queue_res.scalars().all()

        if not waiting_items:
            if assigned_count > 0:
                await db.commit()
            return assigned_count

        for q_item in waiting_items:
            # Skip if this queue item specifically requests an agent that is currently busy
            if q_item.requested_expert_id:
                req_exp = await db.get(Expert, q_item.requested_expert_id)
                if req_exp and req_exp.status not in ["AVAILABLE", "FREE"]:
                    # Respect customer's requested agent preference; do NOT assign to random agent!
                    continue
                elif req_exp and req_exp.status in ["AVAILABLE", "FREE"]:
                    expert = req_exp
                else:
                    expert = None
            else:
                # General queue item: find any available expert
                expert_query = (
                    select(Expert)
                    .where(
                        Expert.business_id == q_item.business_id,
                        Expert.status.in_(["AVAILABLE", "FREE"])
                    )
                    .order_by(Expert.active_lead_count.asc(), Expert.created_at.asc())
                    .with_for_update()
                )
                expert = (await db.execute(expert_query)).scalars().first()

                if not expert:
                    expert = (await db.execute(
                        select(Expert)
                        .where(Expert.status.in_(["AVAILABLE", "FREE"]))
                        .order_by(Expert.active_lead_count.asc(), Expert.created_at.asc())
                        .with_for_update()
                    )).scalars().first()

            if expert and expert.status in ["AVAILABLE", "FREE"]:
                stmt_lead = select(Lead).where(Lead.id == q_item.lead_id).with_for_update()
                lead = (await db.execute(stmt_lead)).scalars().first()
                if not lead:
                    q_item.status = "CANCELLED"
                    continue

                if lead.status == "ASSIGNED" and lead.assigned_expert_id:
                    logger.info(f"[RACE CONDITION PREVENTED] Lead {lead.id} already assigned to {lead.assigned_expert_id}.")
                    q_item.status = "ASSIGNED"
                    q_item.assigned_at = _now()
                    continue

                lead.assigned_expert_id = expert.id
                lead.status = "ASSIGNED"
                expert.status = "BUSY"
                expert.active_lead_count = (expert.active_lead_count or 0) + 1
                q_item.status = "ASSIGNED"
                q_item.assigned_at = _now()

                assignment = LeadAssignment(
                    id=str(uuid.uuid4()),
                    business_id=q_item.business_id,
                    lead_id=lead.id,
                    expert_id=expert.id,
                    strategy="QUEUE_AUTO_ASSIGN",
                    status="ACTIVE",
                    trigger_source=q_item.trigger_source,
                    ai_agent_name="Queue Processor",
                    qualification_stage=lead.qualification_status or "QUALIFIED",
                    notes="Assigned automatically from waiting queue.",
                    context={"queue_item_id": q_item.id},
                    assigned_at=_now(),
                )
                db.add(assignment)
                assigned_count += 1
                logger.info(f"[QUEUE PROCESSOR] Assigned Queued Lead {lead.id} to Expert {expert.name}")

        if assigned_count > 0:
            await db.commit()

        return assigned_count

