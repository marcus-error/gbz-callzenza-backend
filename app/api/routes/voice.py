"""
Module 14 — Voice Calling endpoints.

    POST /api/voice/outbound      trigger an AI call to a lead (auth'd, internal)
    POST /api/voice/answer        Twilio webhook: call connected -> return TwiML
    POST /api/voice/gather        Twilio webhook: caller's speech -> AI reply TwiML
    POST /api/voice/status        Twilio webhook: call status changed
    POST /api/voice/recording     Twilio webhook: recording ready
    POST /api/voice/incoming      Twilio webhook: inbound call to a business number

All Twilio-facing endpoints are unauthenticated (Twilio can't send a JWT)
and instead verify Twilio's request signature — see core/twilio_signature.py.

Every spoken line goes through `speak()`, which synthesizes the audio with
ElevenLabs (using the calling agent's voice_id/stability/similarity_boost)
and has Twilio <Play> it. If ELEVENLABS_API_KEY isn't set, or a synthesis
call fails, it transparently falls back to Twilio's built-in <Say> voice —
so the call flow itself never breaks on a TTS outage.
"""
from __future__ import annotations
import json
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from loguru import logger
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel
from app.core.deps import get_current_business_id, get_current_db_user, require_supervisor_or_admin
from app.core.twilio_signature import verify_twilio_request
from app.db.database import get_db
from app.db.models import AIAgent, AuditLog, Business, Call, Campaign, Conversation, Lead, Message, User
from app.schemas.call import CallOut, TriggerCallRequest
from app.services.ai_service import run_agent
from app.services.lead_engagement_service import process_incoming_message
from app.core.config import get_settings
from app.services.tts_service import synthesize_for_call
from app.services.voice_service import add_client_to_conference_call, build_transcript, initiate_outbound_call

settings = get_settings()

router = APIRouter(prefix="/api/voice", tags=["voice"])

class QualificationRequest(BaseModel):
    qualification_status: str  # QUALIFIED or NOT_QUALIFIED

class AddClientRequest(BaseModel):
    client_name: str | None = None
    client_phone: str | None = None

async def _resolve_or_create_call(db: AsyncSession, id: str, user_id: str, business_id: str) -> Call:
    call = await db.get(Call, id)
    if not call:
        call = await db.scalar(select(Call).where(or_(Call.id == id, Call.call_sid == id)))
    if call:
        if not call.agent_id and user_id:
            call.agent_id = user_id
            db.add(call)
            await db.commit()
        return call

    from app.db.models import LeadAssignment
    assignment = await db.get(LeadAssignment, id)
    if assignment:
        stmt = select(Call).where(Call.lead_id == assignment.lead_id).order_by(Call.created_at.desc())
        existing = await db.scalar(stmt)
        if existing:
            if not existing.agent_id and (user_id or assignment.expert_id):
                existing.agent_id = user_id or assignment.expert_id
                db.add(existing)
                await db.commit()
            return existing
        ctx = assignment.context or {}
        call = Call(
            id=id,
            business_id=assignment.business_id or business_id,
            lead_id=assignment.lead_id,
            agent_id=user_id or assignment.expert_id,
            from_number=settings.twilio_phone_number or "",
            to_number=ctx.get("phone", ""),
            status="HUMAN_HANDLING",
            direction="OUTBOUND",
        )
        db.add(call)
        await db.commit()
        await db.refresh(call)
        return call

    lead = await db.get(Lead, id)
    if lead:
        stmt = select(Call).where(Call.lead_id == lead.id).order_by(Call.created_at.desc())
        existing = await db.scalar(stmt)
        if existing:
            if not existing.agent_id and user_id:
                existing.agent_id = user_id
                db.add(existing)
                await db.commit()
            return existing
        call = Call(
            id=id,
            business_id=lead.business_id or business_id,
            lead_id=lead.id,
            agent_id=user_id,
            from_number=settings.twilio_phone_number or "",
            to_number=lead.phone or "",
            status="HUMAN_HANDLING",
            direction="OUTBOUND",
        )
        db.add(call)
        await db.commit()
        await db.refresh(call)
        return call

    call = Call(
        id=id,
        business_id=business_id,
        lead_id=None,
        agent_id=user_id,
        from_number=settings.twilio_phone_number or "",
        to_number="",
        status="HUMAN_HANDLING",
        direction="OUTBOUND",
    )
    db.add(call)
    await db.commit()
    await db.refresh(call)
    return call


@router.post("/calls/{id}/qualification", response_model=CallOut)
async def update_call_qualification(
    id: str,
    payload: QualificationRequest,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
) -> Call:
    call = await _resolve_or_create_call(db, id, user.id, user.business_id)
    
    old_status = call.status
    if payload.qualification_status == "QUALIFIED":
        call.status = "QUALIFIED"
    else:
        call.status = "NOT_QUALIFIED"

    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    if lead:
        lead.qualification_status = payload.qualification_status
        db.add(lead)

    audit = AuditLog(
        user_id=user.id,
        action="CALL_QUALIFICATION_UPDATED",
        entity="Call",
        entity_id=call.id,
        details={"from_status": old_status, "to_status": call.status, "qualification": payload.qualification_status},
    )
    db.add_all([call, audit])
    await db.commit()

    import json
    from app.api.routes.websockets import global_ws_manager
    lead_name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else "Unknown Lead"
    await global_ws_manager.broadcast(json.dumps({
        "event": "CALL_STATUS_CHANGED",
        "call_id": call.id,
        "lead_name": lead_name,
        "status": call.status,
        "qualification_status": payload.qualification_status,
    }))

    return call


@router.post("/calls/{id}/add-client")
async def trigger_add_client_to_call(
    id: str,
    payload: AddClientRequest | None = None,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    call = await _resolve_or_create_call(db, id, user.id, user.business_id)

    client_name = payload.client_name if (payload and payload.client_name) else call.client_name
    client_phone = payload.client_phone if (payload and payload.client_phone) else call.client_phone

    if not client_phone:
        from app.db.models import CampaignLead
        cmp_lead = await db.scalar(select(CampaignLead).where(CampaignLead.call_id == call.id))
        if cmp_lead:
            campaign = await db.get(Campaign, cmp_lead.campaign_id)
            if campaign:
                client_name = client_name or campaign.client_name
                client_phone = client_phone or campaign.client_phone

    if not client_phone:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="No client phone number configured for this call.")

    import re
    if not re.match(r"^\+?[1-9]\d{1,14}$", client_phone.strip()):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid client phone number: {client_phone}")

    try:
        res = await add_client_to_conference_call(
            db=db,
            call=call,
            client_name=client_name or "Client",
            client_phone=client_phone.strip(),
            user_id=user.id,
        )
        return res
    except ValueError as ve:
        raise HTTPException(status_code=409, detail=str(ve))

MAX_CALL_TURNS = 6


def _xml(vr) -> Response:
    return Response(content=str(vr), media_type="application/xml")


async def broadcast_live_call(call: Call, lead: Lead, message: str):
    try:
        import json
        from app.api.routes.websockets import global_ws_manager
        payload = {
            "call_id": call.id,
            "lead_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip() or lead.phone or "Unknown Lead",
            "agent_id": call.agent_id,
            "status": call.status,
            "duration_seconds": call.duration_seconds or 0,
            "last_message": message,
            "direction": call.direction,
        }
        await global_ws_manager.broadcast(json.dumps(payload))
    except Exception:
        pass


async def speak(container, text: str, agent: AIAgent | None, lang: str | None = None) -> None:
    """Appends spoken audio to a TwiML container (VoiceResponse or Gather).
    Tries ElevenLabs first (agent's configured voice), falls back to
    Twilio's built-in <Say> if no key is set or synthesis fails."""
    import time
    from loguru import logger
    from app.core.languages import get_locale_by_code
    locale = get_locale_by_code(lang) if lang else "en-US"
    tts_start = time.perf_counter()
    audio_url = await synthesize_for_call(text, agent, lang) if agent else None
    logger.info(f"TTS Synthesis Latency: {(time.perf_counter() - tts_start) * 1000:.1f} ms")
    if audio_url:
        container.play(audio_url)
    else:
        import re
        parts = re.split(r'<break\s+time=["\']?(\d*\.?\d+)s?["\']?\s*\/?>|\[pause:?\s*(\d*\.?\d+)s?\]', text, flags=re.IGNORECASE)
        if len(parts) > 1:
            i = 0
            while i < len(parts):
                chunk = (parts[i] or "").strip()
                if chunk:
                    container.say(chunk, language=locale)
                # Next items could be the matched duration groups
                if i + 1 < len(parts) and parts[i + 1]:
                    try:
                        sec = max(1, int(round(float(parts[i + 1]))))
                        container.pause(length=sec)
                    except Exception:
                        pass
                i += 3  # step over regex capture groups
        else:
            container.say(text, language=locale)


# ---------------------------------------------------------------------------
# Internal trigger (auth'd) — module 14 POST /api/v1/voice/outbound
# ---------------------------------------------------------------------------
@router.post("/outbound", response_model=CallOut, status_code=201)
async def trigger_outbound_call(
    payload: TriggerCallRequest,
    business_id: str = Depends(get_current_business_id),
    db: AsyncSession = Depends(get_db),
) -> Call:
    lead = await db.get(Lead, payload.lead_id)
    if not lead or lead.business_id != business_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    if not lead.phone:
        raise HTTPException(status_code=422, detail="Lead has no phone number")

    agent = None
    if payload.agent_id:
        agent = await db.get(AIAgent, payload.agent_id)
    elif lead.agent_id:
        agent = await db.get(AIAgent, lead.agent_id)
    if not agent:
        agent = await db.scalar(select(AIAgent).where(AIAgent.business_id == business_id, AIAgent.status == "ACTIVE"))

    call = await initiate_outbound_call(db, lead, agent)
    await db.commit()
    await db.refresh(call)
    return call


@router.get("/calls", response_model=list[CallOut])
async def list_calls(
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
    lead_id: str | None = None,
    campaign_id: str | None = None,
) -> list:
    try:
        from loguru import logger
        from datetime import datetime, timezone
        from app.db.models import Lead, AIAgent
        stmt = (
            select(Call, Lead.first_name, Lead.last_name, AIAgent.name)
            .outerjoin(Lead, Call.lead_id == Lead.id)
            .outerjoin(AIAgent, Call.agent_id == AIAgent.id)
        )
        if user.role not in ("ADMIN", "SUPER_ADMIN") and user.business_id:
            stmt = stmt.where(Call.business_id == user.business_id)
        if lead_id:
            stmt = stmt.where(Call.lead_id == lead_id)
        if campaign_id:
            stmt = stmt.where(Call.campaign_id == campaign_id)
        stmt = stmt.order_by(Call.created_at.desc())
        result = await db.execute(stmt)
        
        calls_data = []
        for row in result.all():
            call_obj = row[0]
            first_name = row[1] or ""
            last_name = row[2] or ""
            agent_name = row[3] or "Default AI Agent"
            
            c_created = call_obj.created_at
            if isinstance(c_created, str):
                try:
                    c_created = datetime.fromisoformat(c_created)
                except Exception:
                    c_created = datetime.now(timezone.utc)
            elif not c_created:
                c_created = datetime.now(timezone.utc)
                
            c_updated = call_obj.updated_at
            if isinstance(c_updated, str):
                try:
                    c_updated = datetime.fromisoformat(c_updated)
                except Exception:
                    c_updated = datetime.now(timezone.utc)
            elif not c_updated:
                c_updated = datetime.now(timezone.utc)

            calls_data.append({
                "id": str(call_obj.id),
                "business_id": str(call_obj.business_id or ""),
                "lead_id": str(call_obj.lead_id) if call_obj.lead_id else None,
                "campaign_id": str(call_obj.campaign_id) if call_obj.campaign_id else None,
                "agent_id": str(call_obj.agent_id) if call_obj.agent_id else None,
                "conversation_id": str(call_obj.conversation_id) if call_obj.conversation_id else None,
                "call_sid": str(call_obj.call_sid or ""),
                "conference_sid": str(call_obj.conference_sid) if call_obj.conference_sid else None,
                "direction": str(call_obj.direction or "OUTBOUND"),
                "agent_type": str(call_obj.agent_type or "AI_VOICE"),
                "client_name": str(call_obj.client_name or ""),
                "client_phone": str(call_obj.client_phone or ""),
                "from_number": str(call_obj.from_number or ""),
                "to_number": str(call_obj.to_number or ""),
                "status": str(call_obj.status or "QUEUED"),
                "duration_seconds": int(call_obj.duration_seconds or 0),
                "recording_url": str(call_obj.recording_url or f"/recordings/{call_obj.id}_rec.wav"),
                "transcript": str(call_obj.transcript or ""),
                "turn_count": int(call_obj.turn_count or 0),
                "created_at": c_created,
                "updated_at": c_updated,
                "initiated_at": getattr(call_obj, "initiated_at", None),
                "connected_at": getattr(call_obj, "connected_at", None),
                "ended_at": getattr(call_obj, "ended_at", None),
                "formatted_duration": f"{int(call_obj.duration_seconds or 0) // 60:02d}:{int(call_obj.duration_seconds or 0) % 60:02d}",
                "lead_name": f"{first_name} {last_name}".strip() or "Unknown Lead",
                "agent_name": agent_name,
            })
        return calls_data
    except Exception as e:
        from loguru import logger
        logger.error(f"[VOICE LIST CALLS ERROR] {e}")
        return []


@router.get("/calls/{id}", response_model=CallOut)
async def get_call(
    id: str,
    user: User = Depends(get_current_db_user),
    db: AsyncSession = Depends(get_db),
):
    call = await _resolve_or_create_call(db, id, user.id, user.business_id)
    from app.db.models import AIAgent
    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    agent = await db.get(AIAgent, call.agent_id) if call.agent_id else None

    first_name = lead.first_name if lead else ""
    last_name = lead.last_name if lead else ""
    agent_name = agent.name if agent else "Default AI Agent"

    rec_url = call.recording_url or f"/recordings/{call.id}_rec.wav"
    dur_sec = int(call.duration_seconds or 0)

    # Build a plain dict to avoid Pydantic serializing lazy-loaded ORM relationships
    return {
        "id": call.id,
        "business_id": call.business_id,
        "lead_id": call.lead_id,
        "agent_id": call.agent_id,
        "conversation_id": call.conversation_id,
        "call_sid": call.call_sid,
        "conference_sid": getattr(call, "conference_sid", None),
        "direction": call.direction,
        "agent_type": getattr(call, "agent_type", "AI_VOICE"),
        "client_name": getattr(call, "client_name", ""),
        "client_phone": getattr(call, "client_phone", ""),
        "from_number": call.from_number,
        "to_number": call.to_number,
        "status": call.status,
        "duration_seconds": dur_sec,
        "recording_url": rec_url,
        "transcript": call.transcript,
        "turn_count": call.turn_count,
        "created_at": call.created_at,
        "updated_at": call.updated_at,
        "initiated_at": getattr(call, "initiated_at", None),
        "connected_at": getattr(call, "connected_at", None),
        "ended_at": getattr(call, "ended_at", None),
        "formatted_duration": f"{dur_sec // 60:02d}:{dur_sec % 60:02d}",
        "lead_name": f"{first_name} {last_name}".strip() or "Unknown Lead",
        "agent_name": agent_name,
    }


def extract_campaign_script_greeting(
    script_text: str,
    lead: Lead | None = None,
    agent: AIAgent | None = None,
) -> str | None:
    """
    Extracts strictly the opening greeting line from a campaign script using CampaignGreetingService.
    """
    if not script_text or not script_text.strip():
        return None

    from app.services.ai.campaign_greeting_service import CampaignGreetingService
    lead_name = (lead.first_name if lead else "") or ""
    agent_name = (agent.name if agent and agent.name else "Alex")
    company_name = CampaignGreetingService.extract_company_name(script_text)

    return CampaignGreetingService.extract_greeting_from_script(
        script_text=script_text,
        lead_name=lead_name,
        agent_name=agent_name,
        company_name=company_name,
    )



# ---------------------------------------------------------------------------
# Shared greeting logic for /answer and /incoming
# ---------------------------------------------------------------------------
async def _greeting_response(
    db: AsyncSession,
    call: Call,
    lead: Lead,
    agent: AIAgent | None,
    conversation: Conversation,
    lang: str = "en",
    allowed: str = "",
    campaign: Campaign | None = None,
):
    from twilio.twiml.voice_response import Gather, VoiceResponse
    from app.services.lead_engagement_service import format_prompt_for_lead
    from app.core.languages import get_locale_by_code

    # Ensure agent is resolved
    if not agent:
        agent = await db.scalar(select(AIAgent).where(AIAgent.status == "ACTIVE"))
        if not agent:
            agent = await db.scalar(select(AIAgent).order_by(AIAgent.created_at.asc()))

    # Ensure conversation is resolved
    if not conversation:
        conversation = Conversation(
            business_id=call.business_id or (agent.business_id if agent else ""),
            lead_id=lead.id if lead else None,
            agent_id=agent.id if agent else None,
            channel="VOICE",
            status="ACTIVE",
        )
        db.add(conversation)
        await db.flush()
        call.conversation_id = conversation.id

    opening_line = None
    is_inbound = (getattr(call, "direction", "OUTBOUND") or "").upper() == "INBOUND"

    # Resolve official Business / Organization Name
    biz_name = "CALL ZENZA"
    target_biz_id = getattr(call, "business_id", None) or (getattr(campaign, "business_id", None) if campaign else None) or (getattr(agent, "business_id", None) if agent else None)
    if target_biz_id:
        from app.db.models import Business
        biz_obj = await db.get(Business, target_biz_id)
        if biz_obj and biz_obj.name:
            biz_name = biz_obj.name
    elif campaign and campaign.name:
        biz_name = campaign.name

    from app.services.ai.campaign_greeting_service import CampaignGreetingService
    call_direction = "inbound" if is_inbound else "outbound"
    target_campaign = campaign
    if not target_campaign and getattr(call, "campaign_id", None):
        target_campaign = await db.get(Campaign, call.campaign_id)

    opening_line = CampaignGreetingService.resolve_campaign_greeting(
        campaign=target_campaign,
        lead=lead,
        agent=agent,
        call_direction=call_direction,
        language=lang,
        call_id=getattr(call, "id", ""),
    )

    db.add(Message(conversation_id=conversation.id, sender_type="AI", sender_id=agent.id if agent else "", content=opening_line))
    call.turn_count += 1
    call.status = "IN_PROGRESS"
    # NEVER override agent_type to AI_VOICE if this is a human voice agent call
    if getattr(call, "agent_type", None) != "HUMAN_VOICE_AGENT":
        call.agent_type = "AI_VOICE"
    if agent:
        call.agent_id = agent.id
    db.add(call)
    await db.commit()

    await broadcast_live_call(call, lead, f"Agent: {opening_line}")

    # Broadcast CALL_CONNECTED event to WebSockets so Voice Agent Dashboard immediately activates
    try:
        import json
        from datetime import datetime, timezone
        from app.api.routes.websockets import global_ws_manager
        now_iso = datetime.now(timezone.utc).isoformat()
        lead_name_str = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else (call.to_number or "Customer")
        ws_payload = {
            "event": "CALL_CONNECTED",
            "type": "CALL_CONNECTED",
            "call_id": call.id,
            "call_sid": call.call_sid,
            "lead_id": lead.id if lead else call.lead_id,
            "lead_name": lead_name_str,
            "first_name": lead.first_name if lead else "",
            "last_name": lead.last_name if lead else "",
            "lead_phone": lead.phone if lead else call.to_number,
            "phone": lead.phone if lead else call.to_number,
            "destination": call.to_number or (lead.phone if lead else ""),
            "lead_email": getattr(lead, "email", "") or "",
            "lead_company": getattr(lead, "company", "") or "",
            "lead_location": getattr(lead, "location", "") or "",
            "lead_requirement": getattr(lead, "requirement", "") or "",
            "qualification_status": getattr(lead, "qualification_status", "UNQUALIFIED") or "UNQUALIFIED",
            "lead_status": getattr(lead, "status", "CONNECTED") or "CONNECTED",
            "campaign_id": getattr(call, "campaign_id", None) or (campaign.id if campaign else None),
            "status": "CONNECTED",
            "connected_at": now_iso,
            "agent_id": call.agent_id,
            "user_id": call.agent_id,
        }
        await global_ws_manager.broadcast(json.dumps(ws_payload))
    except Exception:
        pass

    locale = get_locale_by_code(lang)
    vr = VoiceResponse()
    gather = Gather(
        input="speech",
        action=f"/api/voice/gather?call_id={call.id}&lang={lang}&allowed={allowed}",
        method="POST",
        speech_timeout="1.1",
        timeout=6,
        barge_in=True,
        language=locale,
    )
    await speak(gather, opening_line, agent, lang)
    vr.append(gather)
    await speak(vr, "We didn't catch a response. We'll follow up another time. Goodbye.", agent, lang)
    vr.hangup()
    return vr


@router.post("/answer")
async def answer_call(request: Request, call_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> Response:
    from app.db.models import CampaignLead, Campaign
    from twilio.twiml.voice_response import VoiceResponse as _VR
    form = dict((await request.form()))
    await verify_twilio_request(request, form)

    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    if form.get("CallSid"):
        call.call_sid = form["CallSid"]

    # =====================================================================
    # CRITICAL RULE: AI MUST NEVER SPEAK ON A HUMAN VOICE AGENT CALL.
    # If this call was initiated by a human voice agent (agent_type =
    # "HUMAN_VOICE_AGENT"), skip ALL AI logic entirely and just keep the
    # call alive so the agent's own phone/SIP can take over.
    # =====================================================================
    agent_type_upper = (getattr(call, "agent_type", "") or "").upper()
    is_human_voice_agent = (
        agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
        or "HUMAN" in agent_type_upper
    )
    if not is_human_voice_agent and getattr(call, "campaign_id", None):
        camp = await db.get(Campaign, call.campaign_id)
        if camp:
            c_type = (camp.agent_type or "").upper()
            if c_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT") or "HUMAN" in c_type or (camp.voice_agent_id and not camp.agent_id):
                is_human_voice_agent = True

    if is_human_voice_agent:
        from loguru import logger as _log
        _log.info(f"[ANSWER_CALL] Call {call_id} is human voice agent (agent_type={call.agent_type}) — skipping AI. Returning silent hold TwiML.")
        # Update call status to HUMAN_HANDLING
        from datetime import datetime, timezone
        call.status = "HUMAN_HANDLING"
        call.connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.add(call)
        if call.lead_id:
            _lead = await db.get(Lead, call.lead_id)
            if _lead:
                _lead.call_status = "CONNECTED"
                db.add(_lead)
        await db.commit()
        # Return TwiML: just wait silently — the human agent's SIP client
        # is already bridged directly via the telephony provider.
        # No AI greeting, no Gather, no STT, no LLM, no TTS.
        _vr = _VR()
        _vr.pause(length=60)  # hold the call alive for up to 60s while SIP bridges
        return _xml(_vr)

    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    agent = await db.get(AIAgent, call.agent_id) if call.agent_id else None
    if not agent:
        agent = await db.scalar(select(AIAgent).where(AIAgent.status == "ACTIVE"))
        if not agent:
            agent = await db.scalar(select(AIAgent).order_by(AIAgent.created_at.asc()))
        if agent:
            call.agent_id = agent.id
            call.agent_type = "AI_VOICE"

    if not lead and (call.to_number or call.from_number):
        phone_num = call.to_number or call.from_number
        lead = await db.scalar(select(Lead).where(Lead.phone == phone_num))
        if not lead:
            lead = Lead(
                business_id=call.business_id or (agent.business_id if agent else ""),
                phone=phone_num,
                first_name="Customer",
                status="ENGAGED",
            )
            db.add(lead)
            await db.flush()
        call.lead_id = lead.id

    conversation = await db.get(Conversation, call.conversation_id) if call.conversation_id else None
    if not conversation:
        conversation = Conversation(
            business_id=call.business_id or (agent.business_id if agent else ""),
            lead_id=lead.id if lead else None,
            agent_id=agent.id if agent else None,
            channel="VOICE",
            status="ACTIVE",
        )
        db.add(conversation)
        await db.flush()
        call.conversation_id = conversation.id

    # Determine campaign-specific default language and script setting
    campaign_lead = await db.scalar(
        select(CampaignLead).where(CampaignLead.call_id == call.id)
    )
    campaign = None
    if campaign_lead:
        campaign = await db.get(Campaign, campaign_lead.campaign_id)
    if not campaign and getattr(call, "campaign_id", None):
        campaign = await db.get(Campaign, call.campaign_id)

    lang = "en"
    allowed_langs = ""
    if campaign and campaign.schedule and isinstance(campaign.schedule, dict):
        lang = campaign.schedule.get("default_language", "en")
        if lang == "Auto Detect":
            lang = "en"
        allowed_list = campaign.schedule.get("allowed_languages", [])
        if isinstance(allowed_list, list):
            allowed_langs = ",".join(allowed_list)
        if agent:
            gender_override = campaign.schedule.get("agent_gender")
            if gender_override:
                if gender_override.lower() == "male":
                    agent.voice_id = "nPczCjzI2devNBz1zQrb"
                else:
                    agent.voice_id = "EXAVITQu4vr4xnSDxMaL"

    vr = await _greeting_response(db, call, lead, agent, conversation, lang=lang, allowed=allowed_langs, campaign=campaign)
    return _xml(vr)


@router.post("/gather")
async def gather_speech(
    request: Request,
    call_id: str = Query(...),
    lang: str = Query("en"),
    allowed: str = Query(""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    from twilio.twiml.voice_response import Gather, VoiceResponse
    from app.core.languages import get_locale_by_code

    form = dict((await request.form()))
    await verify_twilio_request(request, form)

    call = await db.get(Call, call_id)
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")

    # CRITICAL: AI MUST NEVER SPEAK ON A HUMAN VOICE AGENT CALL
    agent_type_upper = (getattr(call, "agent_type", "") or "").upper()
    is_human_voice_agent = (
        agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
        or "HUMAN" in agent_type_upper
    )
    if not is_human_voice_agent and getattr(call, "campaign_id", None):
        camp = await db.get(Campaign, call.campaign_id)
        if camp:
            c_type = (camp.agent_type or "").upper()
            if c_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT") or "HUMAN" in c_type or (camp.voice_agent_id and not camp.agent_id):
                is_human_voice_agent = True

    if is_human_voice_agent:
        from loguru import logger as _log
        from twilio.twiml.voice_response import VoiceResponse as _VR2
        _log.warning(f"[GATHER] Human voice agent call {call_id} reached gather — blocking AI. Returning empty response.")
        _vr2 = _VR2()
        _vr2.pause(length=60)
        return _xml(_vr2)

    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    agent = await db.get(AIAgent, call.agent_id) if call.agent_id else None
    if not agent:
        agent = await db.scalar(select(AIAgent).where(AIAgent.status == "ACTIVE"))
        if not agent:
            agent = await db.scalar(select(AIAgent).order_by(AIAgent.created_at.asc()))

    conversation = await db.get(Conversation, call.conversation_id) if call.conversation_id else None
    if not conversation:
        conversation = Conversation(
            business_id=call.business_id or (agent.business_id if agent else ""),
            lead_id=lead.id if lead else None,
            agent_id=agent.id if agent else None,
            channel="VOICE",
            status="ACTIVE",
        )
        db.add(conversation)
        await db.flush()
        call.conversation_id = conversation.id

    # Load campaign-specific voice override
    from app.db.models import CampaignLead, Campaign
    campaign_lead = await db.scalar(
        select(CampaignLead).where(CampaignLead.call_id == call.id)
    )
    campaign = None
    if campaign_lead:
        campaign = await db.get(Campaign, campaign_lead.campaign_id)
    if not campaign and getattr(call, "campaign_id", None):
        campaign = await db.get(Campaign, call.campaign_id)


    if campaign and campaign.schedule and isinstance(campaign.schedule, dict) and agent:
        gender_override = campaign.schedule.get("agent_gender")
        if gender_override:
            if gender_override.lower() == "male":
                agent.voice_id = "nPczCjzI2devNBz1zQrb"
            else:
                agent.voice_id = "EXAVITQu4vr4xnSDxMaL"

    speech_text = form.get("SpeechResult", "").strip()
    vr = VoiceResponse()

    if not speech_text:
        await speak(vr, "Sorry, I didn't catch that. Thanks for your time — we'll follow up by email. Goodbye.", agent, lang)
        vr.hangup()
        call.status = "COMPLETED"
        conversation.status = "CLOSED" if conversation.status == "ACTIVE" else conversation.status
        call.transcript = await build_transcript(db, conversation.id)
        db.add_all([call, conversation])
        await db.commit()
        return _xml(vr)

    if call.turn_count >= MAX_CALL_TURNS or conversation.status != "ACTIVE":
        await speak(vr, "Thank you — I have everything I need. Our team will follow up shortly. Goodbye.", agent, lang)
        vr.hangup()
        call.status = "COMPLETED"
        conversation.status = "CLOSED" if conversation.status == "ACTIVE" else conversation.status
        call.transcript = await build_transcript(db, conversation.id)
        db.add_all([call, conversation])
        await db.commit()
        return _xml(vr)

    import time
    from loguru import logger
    pipeline_start = time.perf_counter()

    # Pass allowed languages constraints to processing
    allowed_list = [x.strip() for x in allowed.split(",") if x.strip()]
    result = await process_incoming_message(
        db,
        lead,
        conversation,
        agent,
        speech_text,
        current_lang=lang,
        allowed_languages=allowed_list,
        campaign=campaign,
    )
    call.turn_count += 1
    ai_text = result["ai_message"].content if result["ai_message"] else "Thanks for sharing that."
    
    # Extract the new dynamic session language from results
    new_lang = result.get("detected_lang", lang)

    await broadcast_live_call(call, lead, f"Customer: {speech_text}")
    await broadcast_live_call(call, lead, f"Agent: {ai_text}")

    if result["handoff_triggered"] or conversation.status != "ACTIVE":
        await speak(vr, ai_text, agent, new_lang)
        await speak(vr, "I'm connecting you with one of our specialists, who will follow up shortly. Thank you!", agent, new_lang)
        # Instead of hanging up, redirect to hold music loop
        vr.redirect(f"{settings.public_base_url}/api/voice/hold-music", method="GET")
        call.status = "ON_HOLD"
    else:
        locale = get_locale_by_code(new_lang)
        gather = Gather(
            input="speech",
            action=f"/api/voice/gather?call_id={call.id}&lang={new_lang}&allowed={allowed}",
            method="POST",
            speech_timeout="1.1",
            timeout=6,
            barge_in=True,
            language=locale,
        )
        await speak(gather, ai_text, agent, new_lang)
        vr.append(gather)
        await speak(vr, "Thanks for your time. Goodbye.", agent, new_lang)
        vr.hangup()

    total_latency_ms = (time.perf_counter() - pipeline_start) * 1000.0
    logger.info(
        f"[LATENCY METRICS] AUDIO_RECEIVED -> STT -> RAG -> LLM -> TTS -> AUDIO_SENT | "
        f"Call ID: {call.id} | Turn: {call.turn_count} | TOTAL_LATENCY: {total_latency_ms:.1f} ms"
    )
    logger.info(
        f"[CALL:{call.id}] emotion={result.get('emotion', 'neutral')} "
        f"confidence={result.get('emotion_confidence', 0.5):.2f} "
        f"intensity={result.get('emotion_intensity', 0.3):.2f} "
        f"intent={result.get('intent', 'general')} "
        f"tone={result.get('response_tone', 'professional_natural')} "
        f"latency={total_latency_ms:.1f}ms"
    )
    call.transcript = await build_transcript(db, conversation.id)
    db.add_all([call, conversation])
    await db.commit()
    await broadcast_live_call(call, lead, f"Call status: {call.status}")
    return _xml(vr)


@router.post("/status")
async def call_status(request: Request, call_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> dict:
    from datetime import datetime
    form = dict((await request.form()))
    await verify_twilio_request(request, form)

    call = await db.get(Call, call_id)
    if not call:
        return {"ok": True}  # Twilio doesn't need an error here

    status_map = {
        "queued": "QUEUED",
        "initiated": "QUEUED",
        "ringing": "RINGING",
        "in-progress": "IN_PROGRESS",
        "answered": "IN_PROGRESS",
        "completed": "COMPLETED",
        "busy": "BUSY",
        "failed": "FAILED",
        "no-answer": "NO_ANSWER",
        "canceled": "FAILED",
    }
    twilio_status = form.get("CallStatus", "")
    event_call_sid = form.get("CallSid", "")

    # Transfer guard: If call is actively WITH_HUMAN_AGENT, only the customer leg's completion ends the call
    if call.status in ("WITH_HUMAN_AGENT", "HUMAN_HANDLING"):
        if event_call_sid and call.call_sid and event_call_sid != call.call_sid:
            logger.info(
                f"[WEBHOOK_TRANSFER_GUARD] Secondary/AI leg {event_call_sid} completed. "
                f"Customer leg {call.call_sid} remains active with human agent."
            )
            return {"ok": True}

    # 3-Way Conference Guard: Keep CONFERENCE_3WAY_ACTIVE during ringing/in-progress; only primary customer leg hangup terminates all
    if call.status in ("CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING"):
        # If this completion is for an invited/secondary participant leg, keep the conference live for remaining participants!
        if event_call_sid and call.call_sid and event_call_sid != call.call_sid:
            logger.info(
                f"[WEBHOOK_CONFERENCE_GUARD] Secondary/participant leg {event_call_sid} ended with {twilio_status}. "
                f"Customer leg {call.call_sid} and conference remain active."
            )
            return {"ok": True}

        if twilio_status in ("in-progress", "answered", "ringing", "initiated", "queued"):
            call.status = "CONFERENCE_3WAY_ACTIVE"
            db.add(call)
            await db.commit()
            return {"ok": True}
        elif twilio_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            logger.info(f"[3WAY_CONFERENCE_HANGUP] Primary leg {event_call_sid} ended with {twilio_status}. Terminating conference for all.")
            from app.services.voice_service import hangup_sip_call
            hangup_sip_call(call.id)
            call.status = "COMPLETED"

    call.status = status_map.get(twilio_status, call.status)
    if form.get("CallDuration"):
        call.duration_seconds = int(form["CallDuration"])

    if call.status in ("COMPLETED", "BUSY", "FAILED", "NO_ANSWER") and call.conversation_id:
        conversation = await db.get(Conversation, call.conversation_id)
        if conversation and conversation.status == "ACTIVE":
            conversation.status = "CLOSED"
            db.add(conversation)
        call.transcript = await build_transcript(db, call.conversation_id)

    # Mark call ended_at if a terminal status was reached
    if call.status in ("COMPLETED", "BUSY", "FAILED", "NO_ANSWER"):
        if not call.ended_at:
            from datetime import datetime as _dt, timezone as _tz
            call.ended_at = _dt.now(_tz.utc).replace(tzinfo=None)

        # Reset the calling agent's status from ON CALL → AVAILABLE
        if call.agent_id:
            agent_user = await db.get(User, call.agent_id)
            if agent_user and agent_user.status == "ON CALL":
                agent_user.status = "AVAILABLE"
                db.add(agent_user)

    db.add(call)

    # Lead updates
    lead = await db.get(Lead, call.lead_id)
    if lead:
        excel_status_map = {
            "queued": "NEW",
            "initiated": "NEW",
            "ringing": "NEW",
            "in-progress": "CONNECTED",
            "completed": "CONNECTED",
            "busy": "NOT CONNECTED",
            "failed": "NOT CONNECTED",
            "no-answer": "NOT CONNECTED",
            "canceled": "NOT CONNECTED",
            "rejected": "NOT CONNECTED",
        }
        lead.call_status = excel_status_map.get(twilio_status, lead.call_status or "NEW")
        lead.call_date_time = datetime.utcnow()
        if form.get("CallDuration"):
            lead.call_duration = int(form["CallDuration"])
        if call.call_sid:
            lead.twilio_call_sid = call.call_sid
            
        if lead.call_status == "CONNECTED":
            lead.call_result = "CONNECTED"
        elif lead.call_status == "NOT CONNECTED":
            lead.call_result = "NOT CONNECTED"
        else:
            lead.call_result = "—"
            
        db.add(lead)

    # Deterministic CampaignLead matching: exact call_id match first, then campaign_id + lead_id fallback
    from app.db.models import CampaignLead
    cmp_lead = None
    if call.id:
        cmp_lead = (await db.execute(
            select(CampaignLead).where(CampaignLead.call_id == call.id)
        )).scalars().first()
    if not cmp_lead and call.lead_id and call.campaign_id:
        cmp_lead = (await db.execute(
            select(CampaignLead).where(
                CampaignLead.campaign_id == call.campaign_id,
                CampaignLead.lead_id == call.lead_id
            )
        )).scalars().first()

    if cmp_lead:
        cmp_lead_status_map = {
            "completed": "COMPLETED",
            "busy": "BUSY",
            "failed": "FAILED",
            "no-answer": "NO_ANSWER",
            "canceled": "FAILED",
            "in-progress": "CALLING",
        }
        if twilio_status in cmp_lead_status_map:
            cmp_lead.status = cmp_lead_status_map[twilio_status]
            cmp_lead.call_id = call.id
            db.add(cmp_lead)

    # Update DialJob status if exists
    from app.db.models import DialJob
    dial_job = (await db.scalars(select(DialJob).where(DialJob.call_id == call.id))).first()
    if dial_job:
        if twilio_status in status_map:
            dial_job.status = status_map[twilio_status]
        if twilio_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            dial_job.completed_at = datetime.utcnow()
        db.add(dial_job)

    await db.commit()

    if lead and lead.group_id:
        from app.api.routes.lead_groups import sync_excel_file
        await sync_excel_file(db, lead.group_id)

    # Broadcast live status over WebSockets
    try:
        import json
        from datetime import timezone
        from app.api.routes.websockets import global_ws_manager
        if twilio_status in ("in-progress", "answered"):
            now_iso = datetime.now(timezone.utc).isoformat()
            lead_name_str = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else (call.to_number or "Customer")
            await global_ws_manager.broadcast(json.dumps({
                "event": "CALL_CONNECTED",
                "type": "CALL_CONNECTED",
                "call_id": call.id,
                "call_sid": call.call_sid,
                "lead_id": lead.id if lead else call.lead_id,
                "lead_name": lead_name_str,
                "first_name": lead.first_name if lead else "",
                "last_name": lead.last_name if lead else "",
                "lead_phone": lead.phone if lead else call.to_number,
                "phone": lead.phone if lead else call.to_number,
                "destination": call.to_number or (lead.phone if lead else ""),
                "lead_email": getattr(lead, "email", "") or "",
                "lead_company": getattr(lead, "company", "") or "",
                "lead_location": getattr(lead, "location", "") or "",
                "lead_requirement": getattr(lead, "requirement", "") or "",
                "qualification_status": getattr(lead, "qualification_status", "UNQUALIFIED") or "UNQUALIFIED",
                "lead_status": getattr(lead, "status", "CONNECTED") or "CONNECTED",
                "campaign_id": getattr(call, "campaign_id", None) or (cmp_lead.campaign_id if cmp_lead else None),
                "status": "CONNECTED",
                "connected_at": now_iso,
                "agent_id": call.agent_id,
                "user_id": call.agent_id,
            }))
        elif twilio_status == "ringing":
            lead_name_str = f"{lead.first_name or ''} {lead.last_name or ''}".strip() if lead else (call.to_number or "Customer")
            await global_ws_manager.broadcast(json.dumps({
                "event": "CALL_RINGING",
                "type": "CALL_RINGING",
                "call_id": call.id,
                "call_sid": call.call_sid,
                "lead_id": lead.id if lead else call.lead_id,
                "lead_name": lead_name_str,
                "first_name": lead.first_name if lead else "",
                "last_name": lead.last_name if lead else "",
                "lead_phone": lead.phone if lead else call.to_number,
                "phone": lead.phone if lead else call.to_number,
                "destination": call.to_number or (lead.phone if lead else ""),
                "campaign_id": getattr(call, "campaign_id", None) or (cmp_lead.campaign_id if cmp_lead else None),
                "status": "RINGING",
            }))
        elif twilio_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            await global_ws_manager.broadcast(json.dumps({
                "event": "CALL_COMPLETED",
                "type": "CALL_COMPLETED",
                "call_id": call.id,
                "status": call.status,
                "duration_seconds": call.duration_seconds or 0,
                "lead_id": call.lead_id,
                "agent_id": call.agent_id,
            }))
    except Exception:
        pass

    # Re-trigger dialer worker if capacity freed up
    if twilio_status in ("completed", "busy", "failed", "no-answer", "canceled"):
        target_camp_id = getattr(call, "campaign_id", None) or (cmp_lead.campaign_id if cmp_lead else None)
        if target_camp_id:
            try:
                from app.workers.tasks import trigger_campaign_refill
                trigger_campaign_refill(target_camp_id)
            except Exception:
                try:
                    from app.workers.tasks import _run_campaign_calls_async
                    import asyncio
                    asyncio.create_task(_run_campaign_calls_async(target_camp_id))
                except Exception:
                    pass

    return {"ok": True}


@router.post("/recording")
async def call_recording(request: Request, call_id: str = Query(...), db: AsyncSession = Depends(get_db)) -> dict:
    form = dict((await request.form()))
    await verify_twilio_request(request, form)

    call = await db.get(Call, call_id)
    if call and form.get("RecordingUrl"):
        call.recording_url = form["RecordingUrl"]
        db.add(call)
        await db.commit()
    return {"ok": True}


@router.post("/incoming")
async def incoming_call(request: Request, db: AsyncSession = Depends(get_db)) -> Response:
    """Inbound call to a business's Twilio number. Matches (or creates) a
    lead by caller ID within the business that owns the dialed number.
    Resolves configured InboundNumber routing (Voice Agent, User Group, Human Agent)."""
    form = dict((await request.form()))
    await verify_twilio_request(request, form)

    to_number = form.get("To", "")
    from_number = form.get("From", "")

    from app.db.models import InboundNumber, UserGroup, UserGroupMember

    # 1. Resolve configured Campaign and/or InboundNumber record by dialed DID
    clean_to = to_number.lstrip("+0").strip()
    
    # Check Campaign directly by twilio_number
    from app.db.models import Campaign, InboundNumber, UserGroup, UserGroupMember
    camp_stmt = select(Campaign).where(
        or_(
            Campaign.twilio_number == to_number,
            Campaign.twilio_number == f"+{clean_to}",
            Campaign.twilio_number == clean_to,
            Campaign.twilio_number.like(f"%{clean_to}%") if clean_to else False,
        )
    ).order_by(Campaign.created_at.desc())
    campaign = (await db.execute(camp_stmt)).scalars().first()

    # Check InboundNumber table
    inb_stmt = select(InboundNumber).where(
        or_(
            InboundNumber.phone_number == to_number,
            InboundNumber.phone_number == f"+{clean_to}",
            InboundNumber.phone_number == clean_to,
        )
    )
    inbound_config = (await db.execute(inb_stmt)).scalars().first()

    # Determine business/tenant
    business = None
    if campaign:
        business = await db.get(Business, campaign.business_id)
    elif inbound_config:
        business = await db.get(Business, inbound_config.business_id)

    if not business:
        business = await db.scalar(select(Business).where(Business.twilio_number == to_number))
    if not business:
        business = await db.scalar(select(Business).order_by(Business.created_at.asc()))
    if not business:
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("Sorry, this number is not currently configured. Goodbye.")
        vr.hangup()
        return _xml(vr)

    # 2. Check Inbound Enabled Status for Campaign / Line
    is_inbound_active = True
    if campaign and campaign.schedule and isinstance(campaign.schedule, dict):
        if campaign.schedule.get("inbound_enabled") is False:
            is_inbound_active = False
    if inbound_config and inbound_config.status == "INACTIVE":
        is_inbound_active = False

    if not is_inbound_active:
        from twilio.twiml.voice_response import VoiceResponse
        vr = VoiceResponse()
        vr.say("This inbound line is currently inactive. Please try again later. Goodbye.")
        vr.hangup()
        return _xml(vr)

    # 3. Resolve or Create Lead for caller
    lead = await db.scalar(
        select(Lead).where(Lead.business_id == business.id, Lead.phone == from_number).order_by(Lead.created_at.desc())
    )
    if not lead:
        lead = Lead(business_id=business.id, source="INBOUND_PHONE", phone=from_number, status="NEW")
        if campaign and campaign.lead_group_id:
            lead.group_id = campaign.lead_group_id
        db.add(lead)
        await db.flush()

    # 4. Resolve Assigned AI Agent
    agent = None
    if campaign and campaign.agent_id:
        agent = await db.get(AIAgent, campaign.agent_id)
    elif inbound_config and inbound_config.assigned_ai_agent_id:
        agent = await db.get(AIAgent, inbound_config.assigned_ai_agent_id)
    
    if not agent:
        agent = await db.scalar(
            select(AIAgent).where(AIAgent.business_id == business.id, AIAgent.status == "ACTIVE").order_by(AIAgent.created_at.asc())
        )
    if not agent:
        agent = await db.scalar(
            select(AIAgent).where(AIAgent.status == "ACTIVE").order_by(AIAgent.created_at.asc())
        )



    conversation = Conversation(
        business_id=business.id,
        lead_id=lead.id,
        agent_id=agent.id if agent else None,
        channel="VOICE",
        status="ACTIVE",
    )
    db.add(conversation)
    await db.flush()

    call = Call(
        business_id=business.id,
        lead_id=lead.id,
        agent_id=agent.id if agent else None,
        campaign_id=campaign.id if campaign else None,
        conversation_id=conversation.id,
        call_sid=form.get("CallSid", ""),
        direction="INBOUND",
        agent_type="AI_VOICE",
        from_number=from_number,
        to_number=to_number,
        status="IN_PROGRESS",
    )
    db.add(call)
    await db.flush()

    # Broadcast real-time call event to UI WebSockets
    try:
        from app.api.routes.websockets import global_ws_manager
        import json
        await global_ws_manager.broadcast(json.dumps({
            "event": "INCOMING_CALL",
            "type": "CALL_UPDATE",
            "call_id": call.id,
            "campaign_id": campaign.id if campaign else None,
            "direction": "INBOUND",
            "from_number": from_number,
            "to_number": to_number,
            "status": "IN_PROGRESS",
            "agent_name": agent.name if agent else "AI Agent",
            "lead_name": f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Caller",
        }))
    except Exception:
        pass

    vr = await _greeting_response(db, call, lead, agent, conversation, campaign=campaign)
    return _xml(vr)


@router.get("/hold-music")
@router.post("/hold-music")
async def hold_music_twiml() -> Response:
    from twilio.twiml.voice_response import VoiceResponse
    vr = VoiceResponse()
    vr.play(settings.hold_music_url, loop=0)
    return _xml(vr)


@router.get("/join-conference")
@router.post("/join-conference")
async def join_conference_twiml(
    request: Request,
    conf_sid: Optional[str] = Query(None),
    call_id: Optional[str] = Query(None),
    role: Optional[str] = Query("customer"),
) -> Response:
    """
    TwiML endpoint that bridges a call leg into a conference room.
    CRITICAL TRANSFER GUARDS:
    - start_conference_on_enter=True: starts conference immediately
    - end_conference_on_exit=False: CRITICAL: when AI leg disconnects, conference MUST NOT drop!
    - wait_url: plays hold music until the other party bridges in
    """
    from twilio.twiml.voice_response import VoiceResponse, Dial
    vr = VoiceResponse()
    dial = Dial()
    conf_name = conf_sid or (f"CONF-{call_id}" if call_id else "CALLZENZA-BRIDGE")
    
    dial.conference(
        conf_name,
        start_conference_on_enter=True,
        end_conference_on_exit=False,
        wait_url=f"{settings.public_base_url}/api/voice/hold-music",
        status_callback_event="join leave end",
        status_callback=f"{settings.public_base_url}/api/voice/conference/status?conf_sid={conf_name}",
    )
    vr.append(dial)
    return _xml(vr)


@router.get("/resume")
@router.post("/resume")
async def resume_call_twiml(
    request: Request,
    call_id: Optional[str] = Query(None),
) -> Response:
    """Fallback TwiML that keeps call leg alive indefinitely without hanging up, re-attaching media stream."""
    from twilio.twiml.voice_response import VoiceResponse, Start
    vr = VoiceResponse()

    target_id = call_id
    if not target_id:
        try:
            form = dict(await request.form())
            target_id = form.get("call_id") or form.get("CallSid")
        except Exception:
            pass

    if target_id and settings.public_base_url:
        ws_host = settings.public_base_url.replace("http://", "ws://").replace("https://", "wss://")
        customer_stream_url = f"{ws_host}/api/voice/barge/stream/customer/{target_id}"
        start = Start()
        start.stream(url=customer_stream_url, track="both_tracks")
        vr.append(start)

    vr.pause(length=3600)
    return _xml(vr)


@router.post("/conference/status")
async def conference_status_webhook(
    request: Request,
    conf_sid: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Conference participant join/leave webhook logger and 3-way lifecycle coordinator."""
    try:
        form = dict((await request.form()))
        event = (form.get("StatusCallbackEvent") or "").lower()
        part_sid = form.get("CallSid")
        logger.info(f"[CONFERENCE_EVENT] conf={conf_sid} event={event} call_sid={part_sid}")

        if conf_sid:
            stmt = select(Call).where(Call.conference_sid == conf_sid)
            call = (await db.execute(stmt)).scalars().first()
            if not call and "CONF-" in conf_sid:
                raw_id = conf_sid.replace("CONF-", "")
                call = await db.get(Call, raw_id)

            if call and call.status in ("CONFERENCE_3WAY_ACTIVE", "CLIENT_ADDING"):
                if event in ("conference-end", "end"):
                    logger.info(f"[3WAY_CONFERENCE_HANGUP] Conference {conf_sid} ended. Terminating for all parties.")
                    from app.services.voice_service import hangup_sip_call
                    hangup_sip_call(call.id)

                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    call.status = "COMPLETED"
                    call.ended_at = now
                    if not call.client_disconnected_at and call.client_connected_at:
                        call.client_disconnected_at = now
                        call.client_duration_seconds = max(0, int((now - call.client_connected_at).total_seconds()))
                    if call.connected_at:
                        call.duration_seconds = max(1, int((now - call.connected_at).total_seconds()))
                    db.add(call)
                    await db.commit()

                    from app.api.routes.websockets import global_ws_manager
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for evt in ["CALL_ENDED", "CALL_COMPLETED", "REMOTE_HANGUP"]:
                        await global_ws_manager.broadcast(json.dumps({
                            "event": evt,
                            "type": evt,
                            "call_id": call.id,
                            "status": "COMPLETED",
                            "ended_at": now_iso,
                            "duration_seconds": call.duration_seconds or 0,
                        }))
                elif event in ("participant-leave", "leave"):
                    logger.info(f"[3WAY_CONFERENCE_PARTICIPANT_LEAVE] Participant {part_sid} left conference {conf_sid}. Keeping conference live for remaining participants.")
                    now = datetime.now(timezone.utc).replace(tzinfo=None)
                    if not call.client_disconnected_at:
                        call.client_disconnected_at = now
                        if call.client_connected_at:
                            call.client_duration_seconds = max(0, int((now - call.client_connected_at).total_seconds()))
                    call.status = "HUMAN_HANDLING"
                    db.add(call)
                    await db.commit()

                    from app.api.routes.websockets import global_ws_manager
                    await global_ws_manager.broadcast(json.dumps({
                        "event": "PARTICIPANT_LEFT",
                        "type": "PARTICIPANT_LEFT",
                        "call_id": call.id,
                        "participant_sid": part_sid,
                        "status": "HUMAN_HANDLING",
                    }))
                elif event in ("participant-join", "join"):
                    logger.info(f"[3WAY_CONFERENCE_ATTENDED] Participant joined conference {conf_sid}.")
                    if call.status == "CLIENT_ADDING":
                        now_t = datetime.now(timezone.utc).replace(tzinfo=None)
                        call.status = "CONFERENCE_3WAY_ACTIVE"
                        if not call.client_connected_at:
                            call.client_connected_at = now_t
                        db.add(call)
                        await db.commit()

                        from app.api.routes.websockets import global_ws_manager
                        await global_ws_manager.broadcast(json.dumps({
                            "event": "CLIENT_CONNECTED",
                            "call_id": call.id,
                            "status": "CONFERENCE_3WAY_ACTIVE",
                            "client_name": call.client_name,
                            "client_phone": call.client_phone,
                            "client_connected_at": now_t.isoformat(),
                        }))
    except Exception as e:
        logger.warning(f"Conference webhook notice: {e}")
    return {"ok": True}
