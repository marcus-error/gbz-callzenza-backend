import uuid
import copy
import time
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from loguru import logger

from app.db.database import get_db
from app.db.models import Campaign, Agent, AIAgent, Lead, CampaignLead, Call, Business
from app.core.config import get_settings
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.language_service import detect_script_language, detect_speech_language

router = APIRouter(prefix="/api/local-voice-test", tags=["local-voice-test"])

settings = get_settings()

# Lock and active call statuses for preventing race-condition duplicate call initiation
_call_start_lock = asyncio.Lock()

ACTIVE_CALL_STATUSES = [
    "INITIALIZING",
    "QUEUED",
    "RINGING",
    "RINGING_AGENT",
    "IN_PROGRESS",
    "CONNECTED",
    "AI_HANDLING",
    "HUMAN_HANDLING",
    "TALKING",
    "TRANSFER_REQUESTED",
    "TRANSFER_IN_PROGRESS",
    "CONFERENCE_3WAY_ACTIVE",
    "DIALING",
    "CALLING",
    "IN_CALL",
    "RESERVED",
    "initiating",
    "ringing",
    "answered",
    "in_progress",
]



def canonical_phone_key(phone: Any) -> str:
    """Normalizes phone numbers to a canonical digit string (e.g. 10-digit mobile)

    so that +919585310712, 09585310712, and 9585310712 match the exact same target key.
    """
    if not phone:
        return ""
    digits = "".join(c for c in str(phone) if c.isdigit())
    if not digits:
        return ""
    if digits.startswith("91") and len(digits) >= 12:
        digits = digits[2:]
    if digits.startswith("0") and len(digits) >= 11:
        digits = digits.lstrip("0")
    if len(digits) > 10 and digits[-10] in "6789":
        digits = digits[-10:]
    return digits


# In-memory session store for local testing
local_test_sessions: Dict[str, Dict[str, Any]] = {}

DEFAULT_CONVERSATION_STAGES = [
    {"name": "1. Greeting", "status": "CURRENT"},
    {"name": "2. Discovery & Needs", "status": "PENDING"},
    {"name": "3. Solution & Details", "status": "PENDING"},
    {"name": "4. Action & Confirmation", "status": "PENDING"},
]

class StartSessionRequest(BaseModel):
    campaign_id: str
    lead_id: Optional[str] = None
    force: Optional[bool] = False

class MessageRequest(BaseModel):
    session_id: str
    user_message: Optional[str] = None
    message: Optional[str] = None


def sanitize_spoken_dialogue(text: str, agent_name: str = "Alex", lead_name: str = "") -> str:
    """Strips tags, markdown bolding, italics, asterisks, hashtags, and formatting so speech synthesis and display is clean."""
    if not text:
        return ""
    from app.services.ai.response_sanitizer import ResponseSanitizer
    return ResponseSanitizer.clean_customer_facing_text(text, agent_name=agent_name, lead_name=lead_name, cut_off_script_branches=True)


def extract_initial_greeting_utterance(
    script: str,
    agent_name: str = "Alex",
    lead_name: str = "",
    business_name: str = "",
    language: str = "en",
    call_direction: str = "outbound",
) -> str:
    """Extracts strictly the initial opening greeting question/sentence from a raw campaign script
    using CampaignGreetingService."""
    if not script or not script.strip():
        return ""

    from app.services.ai.campaign_greeting_service import CampaignGreetingService
    return CampaignGreetingService.resolve_campaign_greeting(
        lead={"first_name": lead_name} if lead_name else None,
        agent={"name": agent_name} if agent_name else None,
        script_text=script,
        call_direction=call_direction,
        language=language,
    )



def extract_script_stages(
    script_text: str,
    agent_name: str = "AI Voice Agent",
    business_name: str = "our company"
) -> List[Dict[str, str]]:
    """Dynamically parses qualification requirements and stages strictly from the active agent's script."""
    from app.services.ai.qualification_service import QualificationStageEngine
    raw_stages = QualificationStageEngine.extract_stages_from_script(
        script_text=script_text,
        agent_name=agent_name,
        business_name=business_name,
    )
    steps, _, _, _, _, _ = QualificationStageEngine.evaluate_progress(
        stages=raw_stages,
        collected_fields={},
        current_step_idx=0,
    )
    return steps


@router.get("/campaign-leads/{campaign_id}")
async def get_test_campaign_leads(campaign_id: str, db: AsyncSession = Depends(get_db)):
    """Returns assigned leads for a campaign specifically for the local voice tester modal."""
    stmt = (
        select(Lead, CampaignLead)
        .join(CampaignLead, CampaignLead.lead_id == Lead.id)
        .where(CampaignLead.campaign_id == campaign_id)
        .order_by(CampaignLead.created_at.asc())
        .limit(100)
    )
    results = (await db.execute(stmt)).all()
    from app.services.dnd_service import is_number_suppressed
    leads = []
    for lead_row, cl_row in results:
        is_phone_dnd = False
        if lead_row.phone:
            is_phone_dnd = await is_number_suppressed(db, lead_row.phone, getattr(lead_row, "business_id", None))

        is_lead_dnc = (
            is_phone_dnd
            or lead_row.status in ("DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED")
            or cl_row.status in ("DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED")
            or (lead_row.call_result and "dnc" in lead_row.call_result.lower())
        )
        effective_status = "DNC" if is_lead_dnc else (cl_row.status or "PENDING")
        effective_qual = "DNC" if is_lead_dnc else (lead_row.qualification_status or ("QUALIFIED" if effective_status == "QUALIFIED" else "UNQUALIFIED"))

        leads.append({
            "id": lead_row.id,
            "name": f"{lead_row.first_name or ''} {lead_row.last_name or ''}".strip() or "Unnamed Customer",
            "phone": lead_row.phone or "",
            "company": lead_row.company or "",
            "call_status": effective_status,
            "qualification": effective_qual,
            "disposition": effective_status,
            "last_call": cl_row.last_attempt_at.isoformat() if cl_row.last_attempt_at else "Never",
            "duration": 0,
            "agent": "AI Voice Agent",
            "sentiment": "Neutral",
            "intent": "Inquiry",
        })
    return {"campaign_id": campaign_id, "total_returned": len(leads), "leads": leads}


@router.post("/start")
async def start_local_test(body: StartSessionRequest, db: AsyncSession = Depends(get_db)):
    logger.info(f"[CALL API] outbound request received campaign_id={body.campaign_id} lead_id={body.lead_id}")
    try:
        # 1. Load campaign
        campaign_res = await db.execute(select(Campaign).where(Campaign.id == body.campaign_id))
        campaign = campaign_res.scalar_one_or_none()
        if not campaign:
            raise HTTPException(status_code=404, detail="Campaign not found")

        # Determine whether this is an AI Agent or Human Voice Agent campaign
        camp_agent_type = (getattr(campaign, "agent_type", None) or "AI_VOICE").upper()
        transfer_agent_ids = getattr(campaign, "voice_agent_id", None) or "NONE"
        if (
            camp_agent_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
            or "HUMAN" in camp_agent_type
        ):
            effective_agent_type = "HUMAN"
            effective_agent_id = getattr(campaign, "voice_agent_id", None) or getattr(campaign, "user_id", None) or "human_voice_agent"
            is_voice_agent = True
            decision_reason = "campaign_configured_as_human"
        else:
            # AI Agent campaign: Primary handler is AI. Assigned transfer voice agents are strictly transfer backup!
            effective_agent_type = "AI_VOICE"
            effective_agent_id = getattr(campaign, "agent_id", None) or getattr(campaign, "assigned_agent_id", None)
            is_voice_agent = False
            decision_reason = "campaign_configured_as_ai"
            if not effective_agent_id:
                fallback_ai = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
                if not fallback_ai:
                    fallback_ai = (await db.execute(select(AIAgent).order_by(AIAgent.created_at.asc()))).scalars().first()
                if fallback_ai:
                    effective_agent_id = fallback_ai.id

        # 2. Load agent & persona safely
        agent_info = None
        agent_name = "Voice Agent" if is_voice_agent else "AI Voice Assistant"
        agent_lang = "en"
        agent_prompt = None
        target_agent_id = effective_agent_id

        if is_voice_agent:
            if getattr(campaign, "voice_agent_id", None):
                try:
                    from app.db.models import Expert
                    expert_obj = await db.get(Expert, campaign.voice_agent_id)
                    if expert_obj and expert_obj.name:
                        agent_name = expert_obj.name
                        agent_info = {
                            "id": expert_obj.id,
                            "name": expert_obj.name,
                            "role": "Voice Agent",
                            "language": agent_lang,
                        }
                except Exception as e:
                    logger.warning(f"Notice: Expert lookup failed in start_local_test: {e}")
            elif getattr(campaign, "user_id", None):
                try:
                    from app.db.models import User
                    user_obj = await db.get(User, campaign.user_id)
                    if user_obj and (user_obj.name or user_obj.email):
                        agent_name = user_obj.name or user_obj.email.split("@")[0]
                        agent_info = {
                            "id": user_obj.id,
                            "name": agent_name,
                            "role": "Voice Agent",
                            "language": agent_lang,
                        }
                except Exception as e:
                    logger.warning(f"Notice: User lookup failed in start_local_test: {e}")
        else:
            if target_agent_id:
                try:
                    ai_agent = await db.get(AIAgent, target_agent_id)
                    if ai_agent:
                        agent_name = ai_agent.name
                        agent_lang = getattr(ai_agent, "language", "en") or "en"
                        agent_prompt = ai_agent.system_prompt
                        agent_info = {
                            "id": ai_agent.id,
                            "name": ai_agent.name,
                            "role": getattr(ai_agent, "role", "AI Voice Agent"),
                            "system_prompt": ai_agent.system_prompt,
                            "voice_id": getattr(ai_agent, "voice_id", None),
                            "language": agent_lang,
                        }
                except Exception as e:
                    logger.warning(f"Notice: AIAgent lookup failed in start_local_test: {e}")

            if not agent_info and getattr(campaign, "agent_id", None):
                from app.db.models import Agent as LegacyAgent
                try:
                    agent_res = await db.execute(select(LegacyAgent).where(LegacyAgent.id == campaign.agent_id))
                    agent = agent_res.scalar_one_or_none()
                    if agent:
                        agent_name = agent.name
                        agent_lang = "en"
                        agent_prompt = agent.system_prompt
                        agent_info = {
                            "id": agent.id,
                            "name": agent.name,
                            "role": getattr(agent, "role", "AI Voice Agent"),
                            "system_prompt": agent_prompt,
                            "voice_id": getattr(agent, "voice_id", None),
                            "language": agent_lang,
                        }
                except Exception as e:
                    logger.warning(f"Notice: Legacy Agent lookup failed in start_local_test: {e}")

        # 3. Extract script & resolve lead
        session_id = str(uuid.uuid4())
        script_raw = getattr(campaign, "script", None) or (campaign.schedule.get("script") if isinstance(campaign.schedule, dict) else None) or ""
        call_direction = (
            getattr(campaign, "call_direction", None)
            or (campaign.schedule.get("call_direction") if isinstance(campaign.schedule, dict) else None)
            or "outbound"
        ).lower()

        if isinstance(script_raw, dict):
            script_text = script_raw.get("text", "")
        else:
            script_text = str(script_raw)

        # Resolve the specific customer lead for this campaign session (strict campaign scoping)
        init_collected_data: Dict[str, Any] = {}
        target_lead = None
        req_lead_id = getattr(body, "lead_id", None)
        if req_lead_id and str(req_lead_id).strip():
            lead_cand = await db.get(Lead, str(req_lead_id).strip())
            if not lead_cand:
                raise HTTPException(status_code=404, detail=f"Lead '{req_lead_id}' not found.")
            # Strict tenant isolation
            if campaign.business_id and lead_cand.business_id != campaign.business_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"Cross-organization lead access denied: Lead belongs to another organization."
                )
            # Verify explicit assignment to this campaign
            cl_stmt = select(CampaignLead).where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.lead_id == lead_cand.id
            )
            cl_record = (await db.execute(cl_stmt)).scalars().first()
            if not cl_record and campaign.lead_group_id and lead_cand.group_id == campaign.lead_group_id:
                # Lead is part of the campaign's assigned lead group
                target_lead = lead_cand
            elif cl_record:
                target_lead = lead_cand
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"Lead '{lead_cand.first_name or req_lead_id}' is not assigned to campaign '{campaign.name}'."
                )

            # Check DND suppression and terminal statuses
            from app.services.dnd_service import is_number_suppressed
            if target_lead.phone and await is_number_suppressed(db, target_lead.phone, campaign.business_id):
                raise HTTPException(
                    status_code=400,
                    detail=f"Outbound call blocked: Lead '{target_lead.first_name or target_lead.phone}' is on the Do Not Call registry."
                )
            lead_term_status = (target_lead.status or "").upper()
            if lead_term_status in ("DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED"):
                raise HTTPException(
                    status_code=400,
                    detail=f"Outbound call blocked: Lead status is {lead_term_status}."
                )
            if cl_record and cl_record.status in ("COMPLETED", "QUALIFIED", "DNC"):
                if cl_record.status == "DNC":
                    raise HTTPException(
                        status_code=400,
                        detail="Outbound call blocked: Lead is on Do Not Call registry."
                    )
                if not getattr(body, "force", False):
                    raise HTTPException(
                        status_code=400,
                        detail=f"Cannot dial lead: Lead campaign assignment is in terminal state '{cl_record.status}'."
                    )
                else:
                    cl_record.status = "PENDING"
                    await db.commit()

        if not target_lead:
            # Query eligible pending/dialing leads explicitly assigned to this campaign
            stmt_cl = select(CampaignLead).where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.status.in_(["PENDING", "DIALING", "RINGING", "CONNECTED", "IN_CALL", "RESERVED"])
            ).order_by(CampaignLead.created_at.asc())
            cl_row = (await db.execute(stmt_cl)).scalars().first()
            if cl_row and cl_row.lead_id:
                cand = await db.get(Lead, cl_row.lead_id)
                if cand and (not campaign.business_id or cand.business_id == campaign.business_id):
                    target_lead = cand
        if not target_lead:
            stmt_cl_any = select(CampaignLead).where(CampaignLead.campaign_id == campaign.id).order_by(CampaignLead.created_at.asc())
            cl_any_row = (await db.execute(stmt_cl_any)).scalars().first()
            if cl_any_row and cl_any_row.lead_id:
                cand = await db.get(Lead, cl_any_row.lead_id)
                if cand and (not campaign.business_id or cand.business_id == campaign.business_id):
                    target_lead = cand
        if not target_lead and campaign.lead_group_id:
            stmt_l = select(Lead).where(
                Lead.group_id == campaign.lead_group_id,
                Lead.status.notin_(["DNC", "DO_NOT_CALL", "BLACKLISTED", "BLOCKED", "COMPLETED"])
            ).order_by(Lead.created_at.asc())
            if campaign.business_id:
                stmt_l = stmt_l.where(Lead.business_id == campaign.business_id)
            target_lead = (await db.execute(stmt_l)).scalars().first()

        # Strict Campaign Isolation: Do NOT fall back to arbitrary organization leads or dummy UUIDs!
        if not target_lead:
            raise HTTPException(
                status_code=400,
                detail=f"No eligible leads assigned to campaign '{campaign.name}'. Please assign leads before dialing."
            )

        actual_lead_id = target_lead.id
        lead_name = ""
        customer_company = ""
        if target_lead:
            init_collected_data["leadId"] = target_lead.id
            init_collected_data["lead_id"] = target_lead.id
            init_collected_data["first_name"] = target_lead.first_name or ""
            init_collected_data["last_name"] = target_lead.last_name or ""
            raw_lead_name = f"{target_lead.first_name or ''} {target_lead.last_name or ''}".strip() or (target_lead.first_name or "")
            if raw_lead_name.lower().startswith("caller ") or re.search(r'\d{6,}', raw_lead_name):
                lead_name = ""
            else:
                lead_name = raw_lead_name
            init_collected_data["customer_name"] = lead_name or "Customer"
            init_collected_data["phone"] = target_lead.phone or ""
            init_collected_data["email"] = target_lead.email or ""
            customer_company = target_lead.company or ""
            init_collected_data["company"] = customer_company

        # Idempotency guard: prevent duplicate outbound SIP calls if already requested recently
        now_ts = time.time()
        for existing_sess_id, sess_data in list(local_test_sessions.items()):
            if sess_data.get("campaign_id") == campaign.id and sess_data.get("lead_id") == actual_lead_id:
                started_at = sess_data.get("started_at", 0)
                c_status = sess_data.get("telephony_status") or "IN_PROGRESS"
                if not getattr(body, "force", False) and (now_ts - started_at < 60.0) and c_status in ("RINGING", "IN_PROGRESS", "CONNECTED"):
                    logger.warning(f"[LocalVoiceTest] Outbound call BLOCKED: Active session {existing_sess_id} already in progress for lead={actual_lead_id}")
                    raise HTTPException(
                        status_code=409,
                        detail=f"An active call is already in progress for this lead ({lead_name or 'Customer'})."
                    )
                    
        # Add a provisional lock to prevent concurrent double-calls
        logger.info(f"[LOCAL_TEST] start_request_id={session_id} call_id=pending duplicate=false")
        local_test_sessions[session_id] = {
            "campaign_id": campaign.id,
            "lead_id": actual_lead_id,
            "started_at": now_ts,
            "telephony_status": "IN_PROGRESS"
        }

        # Substitute Agent and Lead variables in script
        script_text_filled = script_text.replace("[Agent Name]", agent_name).replace("{agent_name}", agent_name)
        if lead_name:
            for placeholder in ["[Name]", "[Customer Name]", "[Lead Name]", "{customer_name}", "{lead_name}", "{name}"]:
                script_text_filled = script_text_filled.replace(placeholder, lead_name)
        else:
            # Clean dangling titles when no specific name is available
            script_text_filled = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s*\[(?:Name|Customer Name|Lead Name)\]', '', script_text_filled)
            script_text_filled = re.sub(r'\[(?:Name|Customer Name|Lead Name)\]', '', script_text_filled)
            script_text_filled = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.)\s*,', ',', script_text_filled)
            script_text_filled = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.)\s*$', '', script_text_filled).strip()

        script_lang = detect_script_language(script_text_filled, default_lang=agent_lang)

        # 4. Extract Dynamic Script Stages strictly from active script
        from app.services.ai.qualification_service import QualificationStageEngine
        raw_stages = QualificationStageEngine.extract_stages_from_script(
            script_text=script_text_filled,
            agent_name=agent_name,
            business_name=campaign.name,
        )
        script_steps, stage_name, active_idx, qual_status, _, _ = QualificationStageEngine.evaluate_progress(
            stages=raw_stages,
            collected_fields=init_collected_data,
            current_step_idx=0,
            script_text=script_text_filled,
        )

        # 5. Determine initial greeting dynamically from active campaign script via CampaignGreetingService
        from app.services.ai.campaign_greeting_service import CampaignGreetingService
        initial_greeting = CampaignGreetingService.resolve_campaign_greeting(
            campaign=campaign,
            lead=target_lead,
            agent=agent_info,
            script_text=script_text_filled,
            call_direction=call_direction,
            language=script_lang,
            session_id=session_id,
        )
        initial_greeting = sanitize_spoken_dialogue(initial_greeting)

        # 6. Initialize Session Memory & Record Greeting
        ConversationMemoryService.clear_session(effective_agent_id, session_id)
        ConversationMemoryService.set_language(effective_agent_id, session_id, script_lang)
        ConversationMemoryService.set_call_direction(effective_agent_id, session_id, call_direction)
        if not is_voice_agent:
            ConversationMemoryService.mark_greeting_sent(effective_agent_id, session_id)
            ConversationMemoryService.add_turn(effective_agent_id, session_id, role="assistant", content=initial_greeting)
        logger.info(f"[VOICE_SESSION] session_id={session_id} agent_id={effective_agent_id} agent_type={effective_agent_type} campaign_id={campaign.id} direction={call_direction.upper()}")

        customer_phone = init_collected_data.get("phone", "")
        customer_display_name = lead_name or "Customer"
        biz_id = getattr(campaign, "business_id", None) or getattr(campaign, "organization_id", None) or (target_lead.business_id if target_lead else None)

        # Enforce strict backend DND suppression check before placing call or creating call record
        if customer_phone:
            from app.services.dnd_service import is_number_suppressed
            if await is_number_suppressed(db, customer_phone, biz_id):
                logger.warning(f"[OUTBOUND_CALL_SUPPRESSED] Phone '{customer_phone}' is on DND. Blocking local test call.")
                if target_lead:
                    target_lead.status = "DNC"
                    target_lead.call_status = "DNC"
                    target_lead.call_result = "Do Not Call (DNC)"
                    db.add(target_lead)
                    cl_stmt = select(CampaignLead).where(
                        CampaignLead.campaign_id == campaign.id,
                        CampaignLead.lead_id == target_lead.id
                    )
                    cl_row = (await db.execute(cl_stmt)).scalars().first()
                    if cl_row:
                        cl_row.status = "DNC"
                        cl_row.call_result = "Do Not Call (DNC)"
                        db.add(cl_row)
                    await db.commit()
                local_test_sessions.pop(session_id, None)
                raise HTTPException(
                    status_code=403,
                    detail=f"Outbound call blocked: Phone number {customer_phone} is registered on the Do Not Call (DND) list."
                )

        initial_call_id = str(uuid.uuid4())
        telephony_sid = initial_call_id
        telephony_status = "INITIALIZING"
        telephony_error = None
        now_dt = datetime.now(timezone.utc).replace(tzinfo=None)
        _from_num = (
            getattr(campaign, "twilio_number", None)
            or getattr(settings, "sip_username", None)
            or getattr(settings, "sip_did_number", None)
            or getattr(settings, "twilio_phone_number", None)
            or ""
        )

        from app.services.voice_service import get_voice_provider, SIPVoiceProvider, TwilioVoiceProvider
        provider = get_voice_provider()
        telephony_sid = f"local-test-{session_id[:8]}"
        telephony_status = "RINGING" if customer_phone else "IN_PROGRESS"
        telephony_error = None

        # Pre-tag barge session for outbound call
        try:
            from app.api.routes.barge_stream import _get_or_create_session
            sess = await _get_or_create_session(initial_call_id)
            sess["is_dashboard_outbound"] = True
            sess["agent_type"] = effective_agent_type
            sess["is_voice_agent_call"] = is_voice_agent
            sess["campaign_id"] = campaign.id
            sess["primary_agent_id"] = effective_agent_id
            sess["initial_greeting"] = initial_greeting
        except Exception as exc:
            logger.debug(f"Barge session tag note: {exc}")

        logger.info(
            f"[CALL_ROUTING] call_id={initial_call_id} direction={call_direction.upper()} "
            f"campaign_id={campaign.id} campaign_primary_agent_type={camp_agent_type} "
            f"primary_agent_id={effective_agent_id} transfer_agent_ids={transfer_agent_ids} "
            f"resolved_agent_type={effective_agent_type} decision_reason='{decision_reason}' "
            f"ai_bot={'WILL_START' if not is_voice_agent else 'SKIPPED'}"
        )

        # Check DND suppression before initiating call
        from app.services.dnd_service import is_number_suppressed
        if customer_phone and await is_number_suppressed(db, customer_phone, biz_id):
            logger.warning(f"[LocalVoiceTest] Outbound call BLOCKED: Number {customer_phone} is suppressed by DND/DNC.")
            if target_lead:
                target_lead.status = "DNC"
                target_lead.call_status = "DNC"
                target_lead.call_result = "Do Not Call (DNC)"
                db.add(target_lead)
                stmt_cl = select(CampaignLead).where(CampaignLead.campaign_id == campaign.id, CampaignLead.lead_id == target_lead.id)
                cl_rows = (await db.execute(stmt_cl)).scalars().all()
                for cl in cl_rows:
                    cl.status = "DNC"
                    db.add(cl)
                await db.commit()
            raise HTTPException(
                status_code=403,
                detail=f"The phone number ({customer_phone}) is on the Do Not Call (DND) list and cannot be dialed."
            )

        # Atomic active-call check & Call record creation under async lock to prevent 10ms race conditions
        from app.workers.dialer import get_campaign_dispatch_lock, _parse_ratio, count_campaign_active_calls
        async with get_campaign_dispatch_lock(campaign.id):
            from app.services.voice_service import ACTIVE_SIP_CALLS, hangup_sip_call, reconcile_unended_call
            # 1. Enforce campaign-wide concurrency ratio limit
            ratio = _parse_ratio(campaign.auto_dial_ratio)
            target_capacity = max(1, ratio)

            active_count, _ = await count_campaign_active_calls(db, campaign.id)
            if active_count >= target_capacity and not getattr(body, "force", False):
                logger.warning(
                    f"[LocalVoiceTest] Outbound call BLOCKED: Campaign concurrency limit reached "
                    f"({active_count}/{target_capacity}) for campaign_id={campaign.id}"
                )
                raise HTTPException(
                    status_code=429,
                    detail=f"Campaign concurrency limit reached ({active_count}/{target_capacity}). Please wait for active calls to complete."
                )

            stmt_camp_active = select(Call).where(
                Call.campaign_id == campaign.id,
                Call.status.in_(ACTIVE_CALL_STATUSES),
                Call.ended_at.is_(None)
            )
            live_camp_calls = (await db.execute(stmt_camp_active)).scalars().all()

            # 2. Prevent duplicate lead dialing for same lead ID
            if target_lead and not getattr(body, "force", False):
                for active_c in live_camp_calls:
                    if active_c.lead_id == target_lead.id:
                        logger.warning(
                            f"[LocalVoiceTest] Outbound call BLOCKED: Active call already exists for lead {target_lead.id} "
                            f"({target_lead.first_name}). Active call_id={active_c.id} status={active_c.status}"
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=f"An active call is already in progress for this lead ({target_lead.first_name or 'Customer'})."
                        )

            # 3. Prevent duplicate active calls to same phone number
            norm_phone_key = canonical_phone_key(customer_phone)
            if norm_phone_key:
                cutoff_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=15)
                stmt_active = select(Call).where(
                    Call.status.in_(ACTIVE_CALL_STATUSES),
                    Call.ended_at.is_(None),
                    Call.initiated_at >= cutoff_dt,
                )
                active_rows = (await db.execute(stmt_active)).scalars().all()
                for active_c in active_rows:
                    c_phone_key = canonical_phone_key(active_c.client_phone) or canonical_phone_key(active_c.to_number)
                    if c_phone_key and c_phone_key == norm_phone_key:
                        has_sip_session = (
                            active_c.id in ACTIVE_SIP_CALLS
                            or any(
                                v.get("db_call_id") == active_c.id or v.get("call_cid") == active_c.id
                                for v in ACTIVE_SIP_CALLS.values()
                            )
                        )
                        has_local_test_session = any(
                            s.get("call_id") == active_c.id for s in local_test_sessions.values()
                        )
                        age_seconds = (now_dt - active_c.initiated_at).total_seconds() if active_c.initiated_at else 9999
                        is_stale_ringing = active_c.status in ("RINGING", "CALLING", "DIALING", "RESERVED") and age_seconds > 45
                        is_orphaned = not has_sip_session and not has_local_test_session and (age_seconds > 15 or is_stale_ringing)

                        if is_orphaned or is_stale_ringing or getattr(body, "force", False):
                            logger.info(
                                f"[LocalVoiceTest] Auto-reaping stale/orphaned call id={active_c.id} status={active_c.status} "
                                f"age={int(age_seconds)}s for target '{norm_phone_key}' (force={getattr(body, 'force', False)})"
                            )
                            try:
                                hangup_sip_call(active_c.id)
                            except Exception:
                                pass
                            active_c.status = "COMPLETED" if active_c.status in ("CONNECTED", "IN_PROGRESS", "HUMAN_HANDLING") else "FAILED"
                            active_c.ended_at = now_dt
                            active_c.call_result = "NO_ANSWER" if active_c.status == "FAILED" else "COMPLETED"
                            db.add(active_c)
                            if active_c.lead_id:
                                l_obj = await db.get(Lead, active_c.lead_id)
                                if l_obj and l_obj.call_status in ACTIVE_CALL_STATUSES:
                                    l_obj.call_status = active_c.status
                                    db.add(l_obj)
                            await db.commit()
                            continue

                        logger.warning(
                            f"[LocalVoiceTest] Outbound call BLOCKED: Active call already exists for normalized target '{norm_phone_key}' "
                            f"(raw: '{customer_phone}'). Active call_id={active_c.id} status={active_c.status}"
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=f"An active call is already in progress for target phone number ({customer_phone}). Please wait for the current call to finish."
                        )

            # Resolve SIP Account for campaign (Strict enforcement: zero silent fallback to defaults)
            if not getattr(campaign, "sip_account_id", None):
                raise HTTPException(
                    status_code=400,
                    detail=f"Campaign '{campaign.name}' has no outbound SIP account assigned. Please configure an outbound SIP account in Campaign settings before dialing."
                )

            from app.db.models import SIPAccount
            resolved_sip_account = await db.get(SIPAccount, campaign.sip_account_id)
            if not resolved_sip_account:
                raise HTTPException(
                    status_code=400,
                    detail=f"Campaign '{campaign.name}' assigned SIP account ('{campaign.sip_account_id}') does not exist."
                )
            if not resolved_sip_account.enabled:
                raise HTTPException(
                    status_code=400,
                    detail=f"Campaign '{campaign.name}' assigned SIP account '{resolved_sip_account.name}' is currently disabled. Please enable it before dialing."
                )
            if biz_id and resolved_sip_account.organization_id != biz_id:
                raise HTTPException(
                    status_code=403,
                    detail=f"Tenant isolation mismatch: SIP account '{resolved_sip_account.id}' belongs to org '{resolved_sip_account.organization_id}', not '{biz_id}'."
                )

            # Resolve DID / Caller ID: campaign caller_did_id -> SIP account caller_id -> SIP username
            selected_did = None
            if getattr(campaign, "caller_did_id", None):
                from app.db.models import DIDNumber
                did_row = await db.get(DIDNumber, campaign.caller_did_id)
                if did_row and did_row.phone_number:
                    selected_did = did_row.phone_number.strip()
            if not selected_did:
                selected_did = (resolved_sip_account.caller_id or resolved_sip_account.username or "").strip()
            if not selected_did and campaign.twilio_number and campaign.twilio_number != "LOCAL_TEST":
                selected_did = campaign.twilio_number.strip()

            if not selected_did:
                raise HTTPException(
                    status_code=400,
                    detail=f"Campaign '{campaign.name}' has no caller ID or DID configured on its assigned SIP account."
                )

            _from_num = selected_did

            logger.info(
                f"[CAMPAIGN_SIP_ROUTING] campaign_id={campaign.id} lead_id={actual_lead_id} "
                f"call_id={initial_call_id} org_id={biz_id} sip_account_id={resolved_sip_account.id} "
                f"account_name='{resolved_sip_account.name}' selected_did='{_from_num}' "
                f"host={resolved_sip_account.host}:{resolved_sip_account.port} transport={resolved_sip_account.transport}"
            )

            # Pre-create Call record in DB so 200 OK SIP monitor immediately finds it
            try:
                live_call = Call(
                    id=initial_call_id,
                    business_id=biz_id,
                    campaign_id=campaign.id,
                    sip_account_id=resolved_sip_account.id if resolved_sip_account else None,
                    agent_id=effective_agent_id,
                    user_id=getattr(campaign, "user_id", None),
                    lead_id=target_lead.id if target_lead else None,
                    call_sid=telephony_sid,
                    from_number=_from_num,
                    to_number=customer_phone or _from_num,
                    direction="OUTBOUND",
                    agent_type=effective_agent_type,
                    client_name=customer_display_name,
                    client_phone=customer_phone,
                    status="RINGING" if customer_phone else "IN_PROGRESS",
                    duration_seconds=0,
                    initiated_at=now_dt,
                    transcript="" if is_voice_agent else f"AI: {initial_greeting}",
                    turn_count=0,
                    call_result="IN_PROGRESS",
                )
                db.add(live_call)
                if target_lead:
                    target_lead.call_status = "RINGING"
                    db.add(target_lead)
                    if campaign and campaign.id:
                        cl_stmt = select(CampaignLead).where(
                            CampaignLead.campaign_id == campaign.id,
                            CampaignLead.lead_id == target_lead.id
                        )
                        cl_rec = (await db.execute(cl_stmt)).scalars().first()
                        if cl_rec:
                            cl_rec.status = "RINGING"
                            cl_rec.call_id = initial_call_id
                            cl_rec.attempt_count = (cl_rec.attempt_count or 0) + 1
                            cl_rec.last_attempt_at = now_dt
                            db.add(cl_rec)
                await db.commit()
            except Exception as init_call_err:
                logger.warning(f"[LocalVoiceTest] Initial call record creation notice: {init_call_err}")

        if customer_phone and isinstance(provider, (SIPVoiceProvider, TwilioVoiceProvider)):
            try:
                answer_url = f"{settings.public_base_url}/api/voice/answer?call_id={initial_call_id}"
                status_url = f"{settings.public_base_url}/api/voice/status?call_id={initial_call_id}"
                logger.info(f"[LocalVoiceTest] Placing real outbound call to {customer_phone} via {provider.__class__.__name__} (sip_account={getattr(resolved_sip_account, 'name', None)})")
                telephony_sid = await provider.place_call(
                    to=customer_phone,
                    from_=_from_num,
                    answer_url=answer_url,
                    status_callback_url=status_url,
                    sip_account=resolved_sip_account,
                )
                telephony_status = "RINGING"
                logger.info(f"[LocalVoiceTest] Real call successfully placed with SID {telephony_sid}")
            except Exception as tel_err:
                logger.warning(f"[LocalVoiceTest] Real call placement notice: {tel_err}")
                telephony_status = "FAILED"
                telephony_error = str(tel_err)

        # Update Call record with provider telephony SID & status
        try:
            db_call = await db.get(Call, initial_call_id)
            if db_call:
                db_call.call_sid = telephony_sid
                if telephony_status == "FAILED":
                    db_call.status = "FAILED"
                    db_call.call_result = "FAILED"
                    db_call.ended_at = datetime.now(timezone.utc).replace(tzinfo=None)
                db.add(db_call)
            else:
                live_call = Call(
                    id=initial_call_id,
                    business_id=biz_id,
                    campaign_id=campaign.id,
                    agent_id=effective_agent_id,
                    user_id=getattr(campaign, "user_id", None),
                    lead_id=target_lead.id if target_lead else None,
                    call_sid=telephony_sid,
                    from_number=_from_num,
                    to_number=customer_phone or _from_num,
                    direction=call_direction.upper(),
                    agent_type=effective_agent_type,
                    client_name=customer_display_name,
                    client_phone=customer_phone,
                    status=telephony_status,
                    duration_seconds=0,
                    initiated_at=now_dt,
                    connected_at=now_dt if telephony_status in ("IN_PROGRESS", "CONNECTED") else None,
                    ended_at=now_dt if telephony_status == "FAILED" else None,
                    transcript="" if is_voice_agent else f"AI: {initial_greeting}",
                    turn_count=0,
                    call_result="FAILED" if telephony_status == "FAILED" else "IN_PROGRESS",
                )
                db.add(live_call)

            if target_lead:
                target_lead.twilio_call_sid = telephony_sid
                if telephony_status == "FAILED":
                    target_lead.call_status = "FAILED"
                db.add(target_lead)

                cl_stmt = select(CampaignLead).where(
                    CampaignLead.campaign_id == campaign.id,
                    CampaignLead.lead_id == target_lead.id
                )
                cl_obj = (await db.execute(cl_stmt)).scalars().first()
                if cl_obj:
                    cl_obj.status = telephony_status
                    cl_obj.last_attempt_at = now_dt
                    cl_obj.call_id = initial_call_id
                    db.add(cl_obj)

            await db.commit()
        except Exception as update_err:
            logger.warning(f"[LocalVoiceTest] Post-dial call record update notice: {update_err}")

        local_test_sessions[session_id] = {
            "business_id": biz_id,
            "call_id": initial_call_id,
            "lead_id": target_lead.id if target_lead else None,
            "customer_name": customer_display_name,
            "customer_phone": customer_phone,
            "customer_company": customer_company,
            "campaign_id": campaign.id,
            "campaign_name": campaign.name,
            "call_direction": call_direction,
            "agent_id": effective_agent_id,
            "agent_type": effective_agent_type,
            "is_voice_agent_call": is_voice_agent,
            "agent_name": agent_name,
            "script_text": script_text_filled,
            "script_lang": script_lang,
            "conversation_language": script_lang,
            "agent": agent_info,
            "script_stages_meta": raw_stages,
            "script_steps": script_steps,
            "current_stage_index": active_idx,
            "current_stage_name": stage_name,
            "qualification_status": qual_status,
            "collected_data": init_collected_data,
            "history": [] if is_voice_agent else [{"role": "assistant", "content": initial_greeting}],
            "started_at": time.time(),
        }

        # Synthesize audio for initial greeting via centralized SnapServe TTS (AI agent only)
        audio_url = None
        if not is_voice_agent:
            try:
                from app.services.tts_service import synthesize_for_call
                agent_obj = AIAgent(id=effective_agent_id, name=agent_name)
                audio_url = await asyncio.wait_for(
                    synthesize_for_call(initial_greeting, agent_obj, lang=script_lang, is_local_test=True),
                    timeout=15.0
                )
            except Exception as tts_err:
                logger.warning(f"[LocalVoiceTest] Initial greeting TTS generation skipped/error: {type(tts_err).__name__} - {tts_err}")

        return {
            "session_id": session_id,
            "call_id": initial_call_id,
            "telephony_status": telephony_status,
            "telephony_error": telephony_error,
            "campaign_name": campaign.name,
            "lead_id": actual_lead_id,
            "customer_name": init_collected_data.get("customer_name", "Customer"),
            "customer_phone": init_collected_data.get("phone", ""),
            "customer_company": customer_company,
            "agent_name": agent_name,
            "agent_id": effective_agent_id,
            "agent_type": effective_agent_type,
            "is_voice_agent_call": is_voice_agent,
            "text": initial_greeting,
            "response": initial_greeting,
            "response_text": initial_greeting,
            "initial_message": initial_greeting,
            "greeting": initial_greeting,
            "emotion": "happy",
            "should_speak": not is_voice_agent,
            "handoff": False,
            "audio_url": audio_url,
            "current_step": stage_name,
            "script_steps": script_steps,
            "collected_data": init_collected_data,
            "qualification_status": qual_status,
            "total_stages": len(script_steps),
            "stage_index": active_idx,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error in start_local_test for campaign {body.campaign_id}: {e}")
        from app.services.language_service import generate_time_based_greeting
        session_id = str(uuid.uuid4())
        initial_greeting = generate_time_based_greeting(agent_name="AI Assistant", language="en")
        steps = copy.deepcopy(DEFAULT_CONVERSATION_STAGES)
        local_test_sessions[session_id] = {
            "business_id": None,
            "lead_id": str(uuid.uuid4()),
            "campaign_id": body.campaign_id,
            "campaign_name": "Test Campaign",
            "agent_id": "default_agent",
            "agent_name": "AI Assistant",
            "script_text": "",
            "script_lang": "en",
            "conversation_language": "en",
            "agent": None,
            "script_steps": steps,
            "current_stage_index": 0,
            "current_stage_name": "1. Greeting",
            "qualification_status": "IN_PROGRESS",
            "history": [{"role": "assistant", "content": initial_greeting}],
            "started_at": time.time(),
        }
        return {
            "session_id": session_id,
            "campaign_name": "Test Campaign",
            "agent_name": "AI Assistant",
            "agent_id": "default_agent",
            "initial_message": initial_greeting,
            "greeting": initial_greeting,
            "current_step": "1. Greeting",
            "script_steps": steps,
            "collected_data": {},
            "qualification_status": "IN_PROGRESS",
            "total_stages": 4,
            "stage_index": 0,
        }


def detect_talk_to_agent_intent(text: str) -> bool:
    if not text:
        return False
    user_lower = text.lower().strip()

    # AI identity confirmation queries must NOT trigger human expert transfer
    ai_identity_patterns = [
        r'\b(?:are\s+you|ur|you\'re|is\s+this)\s+(?:an?\s+)?(?:ai|bot|virtual|automated)\s*(?:agent)?\b',
        r'\b(?:ai\s+agent|bot\s+agent|virtual\s+agent)\b',
        r'\bwho\s+is\s+this\s+agent\b',
    ]
    for p in ai_identity_patterns:
        if re.search(p, user_lower):
            return False

    # Negative guards: insurance / benefits queries are not agent transfer requests
    if "lump sum" in user_lower or "benefit" in user_lower:
        return False

    phrases = [
        "transfer to voice agent", "transfer to human agent", "transfer to agent",
        "talk to voice agent", "speak to voice agent", "connect to voice agent",
        "talk to human agent", "talk to your agent", "talk to a human agent", "speak to human agent",
        "speak to human", "speak with a person", "transfer call", "transfer my call", "transfer me to human",
        "transfer to human", "transfer to a person", "connect me to human", "connect me to a person",
        "human agent", "real person", "human representative", "live human agent",
        "want a human", "need a human", "talk to a human", "speak with a human",
        "human representative", "talk to real person", "speak to real person",
        "speak to an agent", "talk to an agent", "speak with an agent", "talk with an agent",
        "speak to a representative", "talk to a representative", "want to speak to an agent",
    ]
    if any(p in user_lower for p in phrases):
        return True

    pattern = r'\b(talk|speak|transfer|connect|switch|pass|get|put|assign)\b.*?\b(human|person|representative|agent|rep|expert|someone|somebody|live agent|real person)\b'
    if re.search(pattern, user_lower):
        return True

    return False


def extract_requested_agent_name(text: str) -> Optional[str]:
    if not text:
        return None
    user_lower = text.lower().strip()

    # AI identity confirmation queries are NOT requested human agent names
    ai_identity_patterns = [
        r'\b(?:are\s+you|ur|you\'re|is\s+this)\s+(?:an?\s+)?(?:ai|bot|virtual|automated)\s*(?:agent)?\b',
        r'\b(?:ai\s+agent|bot\s+agent|virtual\s+agent)\b',
        r'\bwho\s+is\s+this\s+agent\b',
    ]
    for p in ai_identity_patterns:
        if re.search(p, user_lower):
            return None

    # Negative guards: insurance / benefits queries are not agent names
    if "lump sum" in user_lower or "benefit" in user_lower:
        return None

    stop_words = {
        "human", "agent", "person", "someone", "representative", "voice", "live", "call", "me", "you",
        "a", "an", "the", "your", "my", "one", "somebody", "support", "sales", "team", "to", "with", "for",
        "us", "them", "this", "that", "now", "please", "can", "could", "would", "should", "ai", "bot", "ryt", "right",
        "speak", "talk", "transfer", "assign", "connect", "switch", "pass", "put", "want", "need", "get", "like",
        "visit", "book", "schedule", "buy", "rent", "see", "check", "know", "find", "ask", "cancel", "change", "pay", "order",
        "lump", "sum", "benefit", "benefits", "policy", "plan", "details", "insurance"
    }

    # Pattern 1: "expert X", "representative X"
    p1 = re.search(r'\b(?:expert|rep|representative|person)\s+([a-zA-Z]+)\b', user_lower)
    if p1:
        cand = p1.group(1).strip()
        if cand not in stop_words and len(cand) >= 2:
            return cand

    # Pattern 2: Extract candidate expert/agent names following explicit transfer verbs (e.g. "talk to Alex", "speak with Sarah")
    matches = re.findall(r'\b(?:talk|speak|transfer|connect|reach|pass|put|assign)\s+(?:to|with|for)\s+(?:the\s+)?(?:human\s+)?([a-zA-Z]+)\b', user_lower)
    for cand in reversed(matches):
        cand = cand.strip()
        if cand not in stop_words and len(cand) >= 2:
            return cand

    return None


@router.post("/message")
async def send_local_test_message(body: MessageRequest, db: AsyncSession = Depends(get_db)):
    logger.info(f"[LocalVoiceTest] Request received for session: {body.session_id}")
    session = local_test_sessions.get(body.session_id)
    if not session:
        logger.warning(f"[LocalVoiceTest] Session {body.session_id} missing. Auto-reinitializing.")
        session = {
            "business_id": None,
            "lead_id": str(uuid.uuid4()),
            "campaign_id": "auto_healed",
            "campaign_name": "Test Campaign",
            "agent_id": "default_agent",
            "agent_name": "AI Assistant",
            "script_steps": copy.deepcopy(DEFAULT_CONVERSATION_STAGES),
            "qualification_status": "IN_PROGRESS",
            "current_stage_index": 0,
            "current_stage_name": "1. Greeting",
            "history": [],
            "started_at": time.time(),
        }
        local_test_sessions[body.session_id] = session

    try:
        turn_start_time = time.perf_counter()
        user_text = body.user_message or body.message or ""
        logger.info(f"[LocalVoiceTest] Customer message: {user_text}")
        session["history"].append({"role": "user", "content": user_text})

        # AI to Human Warm Call Transfer hook (behind feature flag ENABLE_AI_TO_HUMAN_TRANSFER)
        try:
            from app.core.config import settings
            if getattr(settings, "enable_ai_to_human_transfer", False):
                from app.services.call_transfer_service import CallTransferService
                transfer_hook_res = await CallTransferService.check_and_process_customer_utterance(
                    session=session,
                    user_text=user_text,
                    db=db,
                )
                if transfer_hook_res:
                    return transfer_hook_res
        except Exception as handoff_exc:
            logger.warning(f"[Warm Transfer Hook] Safe fallback notice: {handoff_exc}")

        # Step 1: Detect Agent Transfer Intent
        requested_agent_name = extract_requested_agent_name(user_text)
        is_general_transfer = detect_talk_to_agent_intent(user_text)
        is_transfer_intent = bool(requested_agent_name) or is_general_transfer

        if is_transfer_intent:
            from app.services.lead_assignment_service import LeadAssignmentService
            full_transcript = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in session["history"]])
            business_id = session.get("business_id", "default_business")

            # CASE A: Specific Agent Requested by Name
            if requested_agent_name:
                target_expert = await LeadAssignmentService.find_expert_by_name(db, name=requested_agent_name, business_id=business_id)
                if not target_expert:
                    response_text = "I couldn't identify that agent name. Could you please clarify the name of the representative you'd like to reach?"
                    session["history"].append({"role": "assistant", "content": response_text})
                    return {
                        "response": response_text,
                        "current_step": "CLARIFICATION_REQUIRED",
                        "script_steps": session.get("script_steps", []),
                        "collected_data": session.get("collected_data", {}),
                        "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
                        "stage_index": session.get("current_stage_index", 0),
                        "total_stages": len(session.get("script_steps", [])),
                        "is_transferred": session.get("is_transferred", False),
                        "transfer_status": session.get("transfer_status"),
                    }

                session["is_transferred"] = True
                res = await LeadAssignmentService.transfer_lead_to_agent(
                    db=db,
                    business_id=business_id,
                    lead_id=session.get("lead_id", "test_lead"),
                    campaign_id=session.get("campaign_id"),
                    ai_agent_name="CallZenza Voice Agent",
                    qualification_stage=session.get("current_stage_name", "Greeting"),
                    transcript=full_transcript,
                    notes=f"Customer requested transfer: '{user_text}'",
                    collected_answers=session.get("collected_data", {}),
                    requested_expert_name=target_expert.name,
                )
                status = res.get("status", "ASSIGNED")
                session["transfer_status"] = status
                agent_name = res.get("assigned_expert", {}).get("name", target_expert.name)
                session["assigned_expert_name"] = agent_name
                response_text = f"Your call is being transferred to {agent_name}. Please hold while I connect you."
                session["history"].append({"role": "assistant", "content": response_text})
                return {
                    "response": response_text,
                    "current_step": "TRANSFERRED",
                    "script_steps": session.get("script_steps", []),
                    "collected_data": session.get("collected_data", {}),
                    "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
                    "stage_index": session.get("current_stage_index", 0),
                    "total_stages": len(session.get("script_steps", [])),
                    "is_transferred": True,
                    "transfer_status": status,
                    "assigned_expert": res.get("assigned_expert"),
                }

            # CASE B: General Transfer Intent
            session["is_transferred"] = True
            res = await LeadAssignmentService.transfer_lead_to_agent(
                db=db,
                business_id=business_id,
                lead_id=session.get("lead_id") or session.get("campaign_id", "test_lead"),
                campaign_id=session.get("campaign_id"),
                ai_agent_name="CallZenza Voice Agent",
                qualification_stage=session.get("current_stage_name", "Greeting"),
                transcript=full_transcript,
                notes=f"Customer requested general transfer: '{user_text}'",
                collected_answers=session.get("collected_data", {}),
            )
            status = res.get("status", "ASSIGNED")
            session["transfer_status"] = status
            agent_name = res.get("assigned_expert", {}).get("name", "Human Agent")
            response_text = f"Your call is being transferred to {agent_name}. Please hold while I connect you."
            session["history"].append({"role": "assistant", "content": response_text})
            return {
                "response": response_text,
                "current_step": "TRANSFERRED",
                "script_steps": session.get("script_steps", []),
                "collected_data": session.get("collected_data", {}),
                "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
                "stage_index": session.get("current_stage_index", 0),
                "total_stages": len(session.get("script_steps", [])),
                "is_transferred": True,
                "transfer_status": status,
                "assigned_expert": res.get("assigned_expert"),
            }

        # Step 2: Universal AI Agent Reasoning via AgentEngine
        agent_id = session.get("agent_id") or "default_agent"
        engine_res = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message=user_text,
            session_id=body.session_id,
            current_language=session.get("conversation_language", "en"),
            script_text=session.get("script_text", ""),
            call_direction=session.get("call_direction", "outbound"),
        )

        response_text = sanitize_spoken_dialogue(engine_res.response)
        active_lang = engine_res.language
        session["conversation_language"] = active_lang
        session["history"].append({"role": "assistant", "content": response_text})

        # Step 3: Dynamic State & Entity Progression strictly from active script & customer utterances
        from app.services.ai.qualification_service import QualificationStageEngine
        stage_before = session.get("current_stage_name", "Greeting")
        qual_before = copy.deepcopy(session.get("collected_data", {}))
        collected_entities = session.get("collected_data", {})
        # Extract from customer message
        new_extracted = QualificationStageEngine.extract_entities_from_text(user_text, existing_fields=collected_entities)
        collected_entities.update(new_extracted)
        # Merge with engine entities
        collected_entities.update(engine_res.entities_captured)
        session["collected_data"] = collected_entities

        raw_stages = session.get("script_stages_meta")
        if not raw_stages:
            raw_stages = QualificationStageEngine.extract_stages_from_script(
                script_text=session.get("script_text", ""),
                agent_name=session.get("agent_name", "AI Assistant"),
                business_name=session.get("campaign_name", "our company"),
            )
            session["script_stages_meta"] = raw_stages

        current_step_memory_idx = ConversationMemoryService.get_current_step_index(agent_id, body.session_id)

        steps, stage_name, current_idx, qual_status, _, _ = QualificationStageEngine.evaluate_progress(
            stages=raw_stages,
            collected_fields=collected_entities,
            current_step_idx=current_step_memory_idx,
            script_text=session.get("script_text", ""),
        )

        # Prevent backward stage movement unless customer explicitly corrected info
        prev_idx = session.get("current_stage_index", 0)
        if current_idx < prev_idx and not any(w in user_text.lower() for w in ["change", "wrong", "actually", "mistake", "correct"]):
            current_idx = prev_idx
            if prev_idx < len(steps):
                stage_name = steps[prev_idx].get("name", stage_name)

        session["script_steps"] = steps
        session["current_stage_index"] = current_idx
        session["current_stage_name"] = stage_name
        session["qualification_status"] = qual_status
        ConversationMemoryService.set_current_step_index(agent_id, body.session_id, current_idx)

        # Persist collected entities and qualification status directly to DB in real-time
        target_lead_id = session.get("lead_id")
        if target_lead_id:
            try:
                db_lead = await db.get(Lead, target_lead_id)
                if db_lead:
                    c_name = collected_entities.get("customer_name") or collected_entities.get("name")
                    if c_name and db_lead.first_name in ("", "Prospect", "Customer"):
                        parts = c_name.strip().split(" ", 1)
                        db_lead.first_name = parts[0]
                        if len(parts) > 1:
                            db_lead.last_name = parts[1]
                    if collected_entities.get("requirement"):
                        db_lead.requirement = str(collected_entities["requirement"])
                    elif collected_entities.get("service"):
                        db_lead.requirement = str(collected_entities["service"])
                    if collected_entities.get("budget"):
                        db_lead.budget = str(collected_entities["budget"])
                    if collected_entities.get("location"):
                        db_lead.location = str(collected_entities["location"])
                    if collected_entities.get("date") or collected_entities.get("time"):
                        d_val = collected_entities.get("date", "")
                        t_val = collected_entities.get("time", "")
                        db_lead.timeline = f"{d_val} {t_val}".strip()
                    if qual_status in ("QUALIFIED", "NOT_QUALIFIED"):
                        db_lead.qualification_status = qual_status
                        db_lead.status = qual_status
                    db.add(db_lead)
                    await db.commit()
            except Exception as save_lead_err:
                logger.warning(f"[LocalVoiceTest] Live lead entity persistence notice: {save_lead_err}")

        logger.info(
            f"[CONVERSATION_STATE] session_id={body.session_id} stage_before='{stage_before}' "
            f"stage_after='{stage_name}' qualification_before={qual_before} "
            f"qualification_after={collected_entities}"
        )

        # FLOW: AI Agent -> Qualification Completed -> Voice Agent Assignment
        if qual_status == "QUALIFIED" and not session.get("is_qualified_assigned"):
            session["is_qualified_assigned"] = True
            from app.services.lead_assignment_service import LeadAssignmentService
            full_transcript = "\n".join([f"{m['role'].upper()}: {m['content']}" for m in session["history"]])
            try:
                assignment_info = await LeadAssignmentService.transfer_lead_to_agent(
                    db=db,
                    business_id=session.get("business_id") or "default_business",
                    lead_id=session.get("lead_id") or session.get("campaign_id", "test_lead"),
                    campaign_id=session.get("campaign_id"),
                    ai_agent_name="CallZenza Voice Agent",
                    qualification_stage="QUALIFIED",
                    trigger_source="QUALIFIED_LEAD",
                    transcript=full_transcript,
                    notes=f"Lead qualified during local test session {body.session_id}",
                    collected_answers=collected_entities,
                )
                session["transfer_status"] = assignment_info.get("status")
                if assignment_info.get("assigned_expert"):
                    session["assigned_expert_name"] = assignment_info["assigned_expert"].get("name")
            except Exception as e:
                logger.error(f"[QUALIFICATION ASSIGNMENT ERROR] Failed to assign qualified lead: {e}")

        # Step 4: Synthesize Audio (Unified Single-Pass via SnapServe TTS)
        audio_url = None
        tts_start = time.perf_counter()
        agent_obj = AIAgent(id=agent_id, name=session.get("agent_name", "AI Assistant"))
        try:
            from app.services.tts_service import synthesize_for_call
            audio_url = await asyncio.wait_for(
                synthesize_for_call(response_text, agent_obj, lang=active_lang, is_local_test=True),
                timeout=12.0
            )
        except Exception as tts_err:
            logger.warning(f"[LocalVoiceTest] TTS generation skipped/error: {type(tts_err).__name__} - {tts_err}")
        tts_lat = int((time.perf_counter() - tts_start) * 1000)

        # If an active phone call is attached to this test session, stream RTP audio to remote caller (AI Agent calls only)
        call_id = session.get("call_id")
        if call_id and not session.get("is_voice_agent_call"):
            try:
                from app.api.routes.barge_stream import barge_sessions
                from app.services.sip_ai_bot import synthesize_text_to_mulaw, wav_to_mulaw
                from app.services.tts_service import AUDIO_DIR
                b_sess = barge_sessions.get(call_id)
                if b_sess and not b_sess.get("is_voice_agent_call"):
                    reply_mulaw = None
                    # Fast zero-overhead reuse: convert generated WAV on disk directly in 0.5ms
                    if audio_url:
                        fname = audio_url.split("/")[-1].split("?")[0]
                        local_path = AUDIO_DIR / fname
                        if local_path.exists():
                            reply_mulaw = wav_to_mulaw(local_path.read_bytes())
                    if not reply_mulaw:
                        reply_mulaw = await synthesize_text_to_mulaw(response_text, agent=agent_obj, is_local_test=True)
                    if reply_mulaw:
                        logger.info("[SNAPSERVE_TTS] rtp_start")
                        chunk_size = 160
                        for i in range(0, len(reply_mulaw), chunk_size):
                            slice_bytes = reply_mulaw[i:i + chunk_size]
                            if len(slice_bytes) < chunk_size:
                                slice_bytes += b'\xff' * (chunk_size - len(slice_bytes))
                            if "rtp_outbound_queue" in b_sess:
                                b_sess["rtp_outbound_queue"].append(slice_bytes)
                            if "ai_rtp_tx_queue" in b_sess:
                                b_sess["ai_rtp_tx_queue"].append(slice_bytes)
                        logger.info(f"[LocalVoiceTest] Enqueued {len(reply_mulaw)} bytes of response RTP audio to live SIP call {call_id}")
            except Exception as rtp_err:
                logger.warning(f"[LocalVoiceTest] Live RTP forward notice: {rtp_err}")

        total_turn_ms = int((time.perf_counter() - turn_start_time) * 1000)
        emo = getattr(engine_res, "emotion", "neutral") if hasattr(engine_res, "emotion") else "neutral"
        llm_lat = getattr(engine_res, "latency_ms", 0) if hasattr(engine_res, "latency_ms") else 0
        lat_bd = getattr(engine_res, "latency_breakdown", {}) if hasattr(engine_res, "latency_breakdown") else {}
        rag_lat = lat_bd.get("rag_latency", 0)
        stt_lat = lat_bd.get("stt_latency", 0)
        lat_bd["tts_latency"] = tts_lat
        lat_bd["total_turn_latency"] = total_turn_ms

        logger.info(
            f"\n[VOICE_LATENCY]\n"
            f"turn_id={session.get('current_stage_index', 1)}\n"
            f"speech_start=0\n"
            f"speech_end={round(turn_start_time, 3)}\n"
            f"stt_start=0\n"
            f"stt_first_partial=0\n"
            f"stt_final=0\n"
            f"llm_start={round(turn_start_time, 3)}\n"
            f"llm_first_token={round(turn_start_time + (llm_lat / 1000.0), 3)}\n"
            f"llm_complete={round(turn_start_time + (llm_lat / 1000.0), 3)}\n"
            f"tts_start={round(tts_start, 3)}\n"
            f"tts_first_audio={round(tts_start + (tts_lat / 1000.0), 3)}\n"
            f"tts_complete={round(tts_start + (tts_lat / 1000.0), 3)}\n"
            f"audio_send_start={round(time.perf_counter(), 3)}\n"
            f"total_time_to_first_audio={total_turn_ms}ms"
        )

        logger.info(
            f"\n[VOICE_PIPELINE]\n"
            f"STT={stt_lat}ms\n"
            f"LLM={llm_lat}ms\n"
            f"TTS={tts_lat}ms\n"
            f"RAG={rag_lat}ms\n"
            f"TOTAL={total_turn_ms}ms"
        )

        # Check if DND or terminal hangup was triggered
        is_dnd = getattr(engine_res, "is_dnd", False)
        terminal_hangup = getattr(engine_res, "terminal_hangup", False) or getattr(engine_res, "end_call", False)
        call_disposition = getattr(engine_res, "call_disposition", None)

        if is_dnd:
            session["is_dnd"] = True
            session["call_disposition"] = "DNC"
            from app.services.dnd_service import add_to_dnd
            cust_phone = session.get("customer_phone") or session.get("collected_data", {}).get("phone")
            if cust_phone:
                try:
                    await add_to_dnd(
                        db=db,
                        phone=cust_phone,
                        business_id=session.get("business_id"),
                        lead_id=session.get("lead_id"),
                        campaign_id=session.get("campaign_id"),
                        call_id=session.get("call_id"),
                        reason="Customer requested Do Not Call during voice test",
                    )
                except Exception as dnd_e:
                    logger.warning(f"[LocalVoiceTest] DND persistence note: {dnd_e}")

        if terminal_hangup:
            session["terminal_hangup"] = True
            if call_disposition:
                session["call_disposition"] = call_disposition
            session["telephony_status"] = "COMPLETED"
            # Schedule delayed Asterisk hangup after response audio finishes playing
            call_id = session.get("call_id")
            if call_id:
                from app.services.voice_service import hangup_sip_call
                playback_delay = max(2.5, min(12.0, len(response_text) / 14.0))
                disp = session.get("call_disposition") or ("QUALIFIED" if qual_status == "QUALIFIED" else ("DNC" if is_dnd else "COMPLETED"))
                async def _delayed_hangup(cid: str, delay: float, final_disp: str, sess_id: str):
                    await asyncio.sleep(delay)
                    try:
                        hangup_sip_call(cid)
                        logger.info(f"[LocalVoiceTest] Telephony hangup executed for call {cid} after {delay:.1f}s")
                    except Exception as h_err:
                        logger.warning(f"[LocalVoiceTest] Telephony hangup note: {h_err}")

                    # Finalize call in DB and broadcast WebSocket event
                    try:
                        from app.db.database import AsyncSessionLocal
                        from app.db.models import Call, Lead, CampaignLead
                        from app.api.routes.websockets import global_ws_manager
                        import json
                        async with AsyncSessionLocal() as async_db:
                            c_rec = await async_db.get(Call, cid)
                            end_time = datetime.now(timezone.utc).replace(tzinfo=None)
                            if c_rec:
                                c_rec.status = final_disp
                                c_rec.call_result = final_disp
                                c_rec.ended_at = end_time
                                async_db.add(c_rec)

                            sess_obj = local_test_sessions.get(sess_id)
                            ld_id = sess_obj.get("lead_id") if sess_obj else (c_rec.lead_id if c_rec else None)
                            if ld_id:
                                ld_rec = await async_db.get(Lead, ld_id)
                                if ld_rec:
                                    ld_rec.call_status = final_disp
                                    ld_rec.call_result = final_disp
                                    if final_disp in ("QUALIFIED", "DNC"):
                                        ld_rec.qualification_status = final_disp
                                    async_db.add(ld_rec)
                                if sess_obj and sess_obj.get("campaign_id"):
                                    cl_stmt = select(CampaignLead).where(
                                        CampaignLead.campaign_id == sess_obj["campaign_id"],
                                        CampaignLead.lead_id == ld_id
                                    )
                                    cl_rec = (await async_db.execute(cl_stmt)).scalars().first()
                                    if cl_rec:
                                        cl_rec.status = final_disp
                                        cl_rec.call_result = final_disp
                                        async_db.add(cl_rec)
                            await async_db.commit()

                            await global_ws_manager.broadcast(json.dumps({
                                "event": "CALL_ENDED",
                                "call_id": cid,
                                "lead_id": ld_id,
                                "status": final_disp,
                                "state": final_disp,
                                "disposition": final_disp,
                                "ended_at": end_time.isoformat(),
                            }))
                    except Exception as db_h_err:
                        logger.warning(f"[LocalVoiceTest] DB call finalize notice: {db_h_err}")

                asyncio.create_task(_delayed_hangup(call_id, playback_delay, disp, body.session_id))

        return {
            "text": response_text,
            "response": response_text,
            "response_text": response_text,
            "emotion": emo,
            "should_speak": True,
            "handoff": getattr(engine_res, "handoff", False),
            "is_transferred": session.get("is_transferred", False) or getattr(engine_res, "handoff", False),
            "audio_url": audio_url,
            "current_step": stage_name,
            "script_steps": steps,
            "collected_data": collected_entities,
            "qualification_status": qual_status,
            "stage_index": current_idx,
            "total_stages": len(steps),
            "latency_ms": total_turn_ms,
            "latency_breakdown": lat_bd,
            "terminal_hangup": terminal_hangup,
            "end_call": terminal_hangup,
            "is_dnd": is_dnd,
            "call_disposition": session.get("call_disposition") or (qual_status if qual_status in ("QUALIFIED", "NOT_QUALIFIED") else None),
        }

    except Exception as exc:
        agent_name = session.get("agent_name", "the assistant")
        fallback_text = f"Yes, this is {agent_name}. Could you please share more details so I can assist you?"
        session["history"].append({"role": "assistant", "content": fallback_text})
        return {
            "text": fallback_text,
            "response": fallback_text,
            "response_text": fallback_text,
            "emotion": "neutral",
            "should_speak": True,
            "handoff": False,
            "current_step": session.get("current_stage_name", "1. Greeting"),
            "script_steps": session.get("script_steps", []),
            "collected_data": session.get("collected_data", {}),
            "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
            "stage_index": session.get("current_stage_index", 0),
            "total_stages": len(session.get("script_steps", [])),
        }


class TalkToAgentRequest(BaseModel):
    session_id: str
    campaign_id: Optional[str] = None
    lead_id: Optional[str] = None
    notes: Optional[str] = None


@router.post("/talk-to-agent")
async def talk_to_agent_endpoint(body: TalkToAgentRequest, db: AsyncSession = Depends(get_db)):
    session = local_test_sessions.get(body.session_id, {})
    campaign_id = body.campaign_id or session.get("campaign_id")
    lead_id = body.lead_id or session.get("lead_id")
    call_id = session.get("call_id")
    business_id = session.get("business_id", "default_business")
    full_transcript = "\n".join([f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in session.get("history", [])])

    from app.services.lead_assignment_service import LeadAssignmentService
    res = await LeadAssignmentService.transfer_lead_to_agent(
        db=db,
        business_id=business_id,
        lead_id=lead_id or campaign_id or "test_lead",
        campaign_id=campaign_id,
        ai_agent_name="CallZenza AI Agent",
        qualification_stage=session.get("current_stage_name", "Greeting"),
        transcript=full_transcript,
        call_id=call_id,
        notes=body.notes or "Customer clicked Talk to Agent in CallZenza AI Agent modal",
        collected_answers=session.get("collected_data", {}),
    )
    status = res.get("status", "ASSIGNED")
    session["is_transferred"] = True
    session["transfer_status"] = status
    assigned_expert = res.get("assigned_expert") or {}
    agent_name = assigned_expert.get("name") or "Human Agent"
    session["assigned_expert_name"] = agent_name
    response_text = f"Your call is being transferred to {agent_name}. Please hold while I connect you."
    session.setdefault("history", []).append({"role": "assistant", "content": response_text})

    return {
        "success": True,
        "response": response_text,
        "is_transferred": True,
        "transfer_status": status,
        "assigned_expert": assigned_expert,
        "assigned_agent_name": agent_name,
        "target_agent_name": agent_name,
    }


class EndSessionRequest(BaseModel):
    session_id: str
    duration_seconds: Optional[int] = None


@router.post("/end")
async def end_local_test(body: EndSessionRequest, db: AsyncSession = Depends(get_db)):
    session = local_test_sessions.get(body.session_id)
    duration_seconds = 0
    if session:
        started_at = session.get("started_at")
        calc_duration = int(time.time() - started_at) if started_at else 0
        duration_seconds = body.duration_seconds if (body.duration_seconds is not None and body.duration_seconds > 0) else calc_duration
        
        history = session.get("history", [])
        transcript_text = "\n".join(
            f"{'AI' if m['role'] == 'assistant' else 'User'}: {m['content']}"
            for m in history
        )
        
        qual_status = session.get("qualification_status")
        session_disp = session.get("call_disposition")
        if session_disp == "DNC" or session.get("is_dnd"):
            call_result = "Do Not Call (DNC)"
            final_lead_status = "DNC"
        elif qual_status == "QUALIFIED" or session_disp == "QUALIFIED":
            call_result = "QUALIFIED"
            final_lead_status = "QUALIFIED"
        elif qual_status == "NOT_QUALIFIED" or session_disp == "NOT_QUALIFIED":
            call_result = "NOT_QUALIFIED"
            final_lead_status = "NOT_QUALIFIED"
        else:
            call_result = "COMPLETED"
            final_lead_status = "CALLED"
        
        try:
            from app.db.models import Call, Business
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            biz_id = session.get("business_id")
            if not biz_id:
                try:
                    biz_stmt = select(Business.id)
                    biz_id = (await db.execute(biz_stmt)).scalar()
                except Exception:
                    biz_id = None

            started_dt = datetime.fromtimestamp(started_at, tz=timezone.utc).replace(tzinfo=None) if started_at else now

            _from = settings.twilio_phone_number or ""
            target_lead_id = session.get("lead_id")
            customer_name = session.get("customer_name") or session.get("campaign_name") or "Campaign Lead"
            customer_phone = (
                session.get("customer_phone")
                or session.get("customer_number")
                or session.get("phone")
                or session.get("to_number")
                or ""
            )
            existing_call_id = session.get("call_id")
            is_transferred = bool(session.get("is_transferred") or session.get("telephony_status") == "HUMAN_HANDLING")

            # Immediately terminate the live Asterisk SIP call ONLY if not transferred to human voice agent
            if not is_transferred:
                try:
                    from app.services.voice_service import hangup_sip_call
                    if existing_call_id:
                        hangup_sip_call(existing_call_id)
                    hangup_sip_call(body.session_id)
                except Exception as h_err:
                    logger.warning(f"[LocalVoiceTest] Live call hangup notice: {h_err}")

            saved_call = await db.get(Call, existing_call_id) if existing_call_id else None
            if not saved_call:
                saved_call = Call(
                    id=str(uuid.uuid4()),
                    business_id=biz_id,
                    campaign_id=session.get("campaign_id"),
                    agent_id=session.get("agent_id"),
                    lead_id=target_lead_id,
                    call_sid=f"local-test-{body.session_id[:8]}",
                    from_number=_from,
                    to_number=customer_phone or _from,
                    direction="OUTBOUND",
                    agent_type="AI_VOICE",
                    client_name=customer_name,
                    client_phone=customer_phone,
                    status="HUMAN_HANDLING" if is_transferred else final_lead_status,
                    duration_seconds=max(1, duration_seconds),
                    initiated_at=started_dt,
                    ended_at=None if is_transferred else now,
                    transcript=transcript_text,
                    turn_count=len([m for m in history if m["role"] == "user"]),
                    call_result="TRANSFERRED" if is_transferred else call_result,
                )
                db.add(saved_call)
            else:
                if not is_transferred:
                    saved_call.status = final_lead_status
                    saved_call.ended_at = now
                    saved_call.call_result = call_result
                saved_call.duration_seconds = max(1, duration_seconds)
                saved_call.transcript = transcript_text
                saved_call.turn_count = len([m for m in history if m["role"] == "user"])
                saved_call.client_name = customer_name
                saved_call.client_phone = customer_phone
                if target_lead_id:
                    saved_call.lead_id = target_lead_id
                db.add(saved_call)

            # Synchronize CampaignLead and Lead statuses
            if target_lead_id:
                try:
                    cl_stmt = select(CampaignLead).where(
                        CampaignLead.campaign_id == session.get("campaign_id"),
                        CampaignLead.lead_id == target_lead_id
                    )
                    cl_res = await db.execute(cl_stmt)
                    cl_record = cl_res.scalars().first()
                    if cl_record:
                        cl_record.status = final_lead_status
                        cl_record.attempt_count = (cl_record.attempt_count or 0) + 1
                        cl_record.last_attempt_at = now
                        cl_record.call_id = saved_call.id
                        db.add(cl_record)

                    lead_obj = await db.get(Lead, target_lead_id)
                    if lead_obj:
                        lead_obj.status = final_lead_status
                        lead_obj.call_status = final_lead_status
                        lead_obj.call_result = call_result
                        if final_lead_status in ("QUALIFIED", "DNC"):
                            lead_obj.qualification_status = final_lead_status
                        db.add(lead_obj)
                except Exception as lead_err:
                    logger.warning(f"[LocalVoiceTest] Lead status update error: {lead_err}")

            await db.commit()
            await db.refresh(saved_call)
            logger.info(f"[LocalVoiceTest] Saved call record id={saved_call.id} for session {body.session_id}")

            try:
                from app.api.routes.websockets import global_ws_manager
                import json
                await global_ws_manager.broadcast(json.dumps({
                    "event": "CALL_ENDED",
                    "call_id": saved_call.id,
                    "lead_id": target_lead_id,
                    "status": final_lead_status,
                    "state": final_lead_status,
                    "disposition": final_lead_status,
                    "duration_seconds": saved_call.duration_seconds,
                    "ended_at": now.isoformat(),
                }))
            except Exception as ws_err:
                logger.debug(f"[LocalVoiceTest] WS broadcast note on end: {ws_err}")
        except Exception as e:
            try:
                await db.rollback()
            except Exception:
                pass
            logger.error(f"[LocalVoiceTest] Failed to save call record: {e}")
        
        # Clear ConversationMemoryService
        agent_id = session.get("agent_id") or "default_agent"
        camp_id = session.get("campaign_id")
        ConversationMemoryService.clear_session(agent_id, body.session_id)
        del local_test_sessions[body.session_id]

    return {"status": "success", "message": "Test session ended", "duration_seconds": duration_seconds}

