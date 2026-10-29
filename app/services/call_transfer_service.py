from __future__ import annotations

import json
import uuid
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.config import settings
from app.api.routes.websockets import global_ws_manager
from app.repositories.call_transfer_repository import CallTransferRepository, CallTransferSession
from app.services.agent_availability_service import AgentAvailabilityService
from app.services.human_handoff_intent import HumanHandoffIntentDetector, IntentDetectionResult
from app.db.models import Call, Lead, Campaign, User


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CallTransferService:
    """
    Manages warm handoff lifecycle from CallZenza AI Agent to Human Voice Agent.
    Guarantees:
    1. Customer leg is kept alive at all times (never hung up during transfer).
    2. Atomic agent reservation locks prevent concurrent double-booking.
    3. Multi-lingual intent detection across English, Tamil, Tanglish, and Hindi.
    4. Automatic graceful fallback to AI agent if no agent is available or accepts.
    """

    MAX_RETRIES = 3
    DEFAULT_TIMEOUT_SECONDS = 20

    @classmethod
    async def check_and_process_customer_utterance(
        cls,
        session: Dict[str, Any],
        user_text: str,
        db: AsyncSession,
    ) -> Optional[Dict[str, Any]]:
        """
        Minimal non-invasive hook point called on customer utterances.
        If transfer intent is detected and feature flag is ON, initiates handoff.
        """
        if not getattr(settings, "enable_ai_to_human_transfer", False):
            return None

        # Guard: Check if transfer was already executed or initiated for this session
        if session.get("is_transferred") or session.get("transfer_initiated"):
            return None

        # Step 1: Detect Intent
        intent_res: IntentDetectionResult = HumanHandoffIntentDetector.detect(user_text)
        if not intent_res.has_intent or intent_res.confidence < 0.60:
            return None

        # Step 2: Handle Medium Confidence (Ask Confirmation)
        if intent_res.requires_confirmation and not session.get("transfer_confirm_asked"):
            session["transfer_confirm_asked"] = True
            confirm_msg = "Would you like me to connect you with a member of our specialist team right now?"
            if intent_res.language == "ta":
                confirm_msg = "உங்களை எங்கள் நிபுணருடன் இணைக்க வேண்டுமா?"
            elif intent_res.language == "hi":
                confirm_msg = "क्या आप हमारी टीम के विशेषज्ञ से बात करना चाहते हैं?"

            session["history"].append({"role": "assistant", "content": confirm_msg})
            return {
                "response": confirm_msg,
                "current_step": "TRANSFER_CONFIRMATION_REQUESTED",
                "script_steps": session.get("script_steps", []),
                "collected_data": session.get("collected_data", {}),
                "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
                "stage_index": session.get("current_stage_index", 0),
                "total_stages": len(session.get("script_steps", [])),
                "is_transferred": False,
                "transfer_status": "CONFIRMATION_PENDING",
            }

        # Step 3: High Confidence -> Initiate Warm Transfer
        session["transfer_initiated"] = True
        call_id = session.get("call_id") or session.get("session_id") or f"call_{uuid.uuid4().hex[:8]}"
        campaign_id = session.get("campaign_id")
        lead_id = session.get("lead_id")
        customer_name = session.get("customer_name") or "Customer"
        customer_phone = session.get("customer_phone") or ""

        # Prepare summary / transcript so far
        history_lines = [f"{m.get('role', 'user').upper()}: {m.get('content', '')}" for m in session.get("history", [])]
        full_transcript = "\n".join(history_lines)

        assigned_target_id = None
        assigned_expert_data = None
        try:
            from app.services.lead_assignment_service import LeadAssignmentService
            la_res = await LeadAssignmentService.transfer_lead_to_agent(
                db=db,
                business_id=session.get("business_id", "default"),
                lead_id=lead_id or campaign_id or "test_lead",
                campaign_id=campaign_id,
                ai_agent_name=session.get("agent_name") or "CallZenza Voice Agent",
                qualification_stage=session.get("current_stage_name", "Greeting"),
                transcript=full_transcript,
                call_id=call_id,
                notes=f"Customer requested human agent: '{user_text}'",
                collected_answers=session.get("collected_data", {}),
            )
            if la_res and la_res.get("assigned_expert"):
                assigned_expert_data = la_res.get("assigned_expert")
                assigned_target_id = assigned_expert_data.get("id") or assigned_expert_data.get("user_id")
        except Exception as la_exc:
            logger.warning(f"[CallTransferService] Lead assignment integration notice: {la_exc}")

        transfer_result = await cls.initiate_warm_transfer(
            call_id=call_id,
            session_id=session.get("session_id"),
            business_id=session.get("business_id", "default"),
            campaign_id=campaign_id,
            lead_id=lead_id,
            customer_name=customer_name,
            customer_phone=customer_phone,
            language=intent_res.language,
            notes=f"Customer requested human agent: '{user_text}'",
            transcript=full_transcript,
            target_agent_id=assigned_target_id,
            db=db,
        )

        session["is_transferred"] = not transfer_result.get("is_fallback", False)
        session["transfer_status"] = transfer_result.get("status")
        session["transfer_id"] = transfer_result.get("transfer_id")
        session["assigned_agent_name"] = transfer_result.get("assigned_agent_name") or (assigned_expert_data.get("name") if assigned_expert_data else None)

        reply_text = transfer_result.get("message")
        session["history"].append({"role": "assistant", "content": reply_text})

        return {
            "response": reply_text,
            "current_step": "TRANSFERRED" if not transfer_result.get("is_fallback") else "QUALIFICATION",
            "script_steps": session.get("script_steps", []),
            "collected_data": session.get("collected_data", {}),
            "qualification_status": session.get("qualification_status", "IN_PROGRESS"),
            "stage_index": session.get("current_stage_index", 0),
            "total_stages": len(session.get("script_steps", [])),
            "is_transferred": not transfer_result.get("is_fallback", False),
            "transfer_status": transfer_result.get("status"),
            "transfer_id": transfer_result.get("transfer_id"),
            "assigned_agent_name": session["assigned_agent_name"],
            "assigned_expert": assigned_expert_data,
            "is_fallback": transfer_result.get("is_fallback", False),
        }

    @classmethod
    async def initiate_warm_transfer(
        cls,
        call_id: str,
        business_id: str,
        session_id: Optional[str] = None,
        campaign_id: Optional[str] = None,
        lead_id: Optional[str] = None,
        customer_name: Optional[str] = "Customer",
        customer_phone: Optional[str] = "",
        language: str = "en",
        notes: Optional[str] = "",
        transcript: Optional[str] = "",
        target_agent_id: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """
        Step 1: Finds available Voice Agent (prioritizing assigned agent on campaign or lead).
        Step 2: Atomically reserves agent.
        Step 3: Mutes AI / places hold message on customer leg without dropping.
        Step 4: Emits real-time WebSocket ringing events.
        """
        if not target_agent_id and db:
            if campaign_id:
                try:
                    from app.db.models import Campaign
                    camp_obj = await db.get(Campaign, campaign_id)
                    if camp_obj and camp_obj.voice_agent_id:
                        target_agent_id = camp_obj.voice_agent_id
                except Exception:
                    pass
            if not target_agent_id and lead_id:
                try:
                    from app.db.models import Lead
                    lead_obj = await db.get(Lead, lead_id)
                    if lead_obj and lead_obj.assigned_expert_id:
                        target_agent_id = lead_obj.assigned_expert_id
                except Exception:
                    pass

        # Step 1: Find & reserve agent
        reserved_agent = await AgentAvailabilityService.find_and_reserve_available_agent(
            call_id=call_id,
            business_id=business_id,
            db=db,
            preferred_agent_id=target_agent_id,
            ttl_seconds=cls.DEFAULT_TIMEOUT_SECONDS,
        )

        now_iso = _now().isoformat()

        # CASE A: No Voice Agent is currently available -> Fallback to AI immediately
        if not reserved_agent:
            fallback_msg = (
                "All our human specialists are currently assisting other customers. "
                "I will arrange a priority callback for you, or we can continue our conversation right now."
            )
            if language == "ta":
                fallback_msg = "எங்கள் அனைத்து நிபுணர்களும் தற்போது அழைப்பில் உள்ளனர். உங்களுக்கு விரைவில் அழைப்பு வர ஏற்பாடு செய்கிறேன், அல்லது நாம் உரையாடலைத் தொடரலாம்."
            elif language == "hi":
                fallback_msg = "हमारे सभी विशेषज्ञ अभी अन्य कॉलों में व्यस्त हैं। मैं आपके लिए कॉलबैक का प्रबंध करूँगा, या हम अभी बात जारी रख सकते हैं।"

            # Broadcast failure / fallback event
            fallback_event = {
                "event": "transfer.failed",
                "type": "transfer.failed",
                "call_id": call_id,
                "session_id": session_id or call_id,
                "reason": "NO_AGENTS_AVAILABLE",
                "message": fallback_msg,
                "timestamp": now_iso,
            }
            await global_ws_manager.broadcast_to_business(business_id or "default", fallback_event)
            await global_ws_manager.broadcast(fallback_event)

            # Record session as fallback
            session_rec = await CallTransferRepository.create_transfer_session(
                call_id=call_id,
                business_id=business_id,
                campaign_id=campaign_id,
                lead_id=lead_id,
                customer_name=customer_name or "Customer",
                customer_phone=customer_phone or "",
                assigned_agent_id=None,
                assigned_agent_name=None,
                transcript=transcript,
                db=db,
            )
            await CallTransferRepository.update_transfer_session_status(
                transfer_id=session_rec.id,
                status="FAILED",
                failure_reason="NO_AGENTS_AVAILABLE",
                db=db,
            )

            return {
                "transfer_id": session_rec.id,
                "call_id": call_id,
                "status": "FAILED",
                "is_fallback": True,
                "message": fallback_msg,
                "assigned_agent_name": None,
            }

        # CASE B: Agent reserved -> Create transfer record and start ringing
        assigned_id = reserved_agent["agent_id"]
        assigned_name = reserved_agent["agent_name"]

        transfer_session = await CallTransferRepository.create_transfer_session(
            call_id=call_id,
            business_id=business_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            customer_name=customer_name or "Customer",
            customer_phone=customer_phone or "",
            assigned_agent_id=assigned_id,
            assigned_agent_name=assigned_name,
            transcript=transcript,
            db=db,
        )

        # Place customer hold music / pause AI without dropping customer leg
        try:
            call_obj = await db.get(Call, call_id)
            if call_obj:
                call_obj.status = "TRANSFER_REQUESTED"
                db.add(call_obj)
                await db.commit()
                from app.services.hold_music_service import start_customer_hold
                await start_customer_hold(db, call_obj)
        except Exception as e:
            logger.warning(f"[CallTransferService] Hold music notice: {e}")

        # Real-time WebSocket Events: transfer.requested and transfer.ringing
        transfer_payload = {
            "event": "transfer.requested",
            "type": "transfer.requested",
            "transfer_id": transfer_session.id,
            "call_id": call_id,
            "session_id": session_id or call_id,
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "target_agent_id": assigned_id,
            "target_agent_name": assigned_name,
            "transcript_summary": (transcript or "")[-600:],
            "notes": notes,
            "timeout_seconds": cls.DEFAULT_TIMEOUT_SECONDS,
            "timestamp": now_iso,
        }

        ringing_payload = {
            **transfer_payload,
            "event": "transfer.ringing",
            "type": "transfer.ringing",
        }

        # Send directly to the target agent
        await global_ws_manager.send_to_user(assigned_id, transfer_payload)
        await global_ws_manager.send_to_user(assigned_id, ringing_payload)

        # Broadcast to business channel
        await global_ws_manager.broadcast_to_business(business_id or "default", transfer_payload)
        await global_ws_manager.broadcast_to_business(business_id or "default", ringing_payload)
        await global_ws_manager.broadcast(transfer_payload)
        await global_ws_manager.broadcast(ringing_payload)

        logger.info(f"[CallTransfer] Dispatched transfer {transfer_session.id} to agent {assigned_name} ({assigned_id})")

        hold_speech = (
            f"I understand. I am transferring your call to {assigned_name}. "
            "Please hold on while I connect you."
        )
        if language == "ta":
            hold_speech = f"நான் உங்கள் அழைப்பை {assigned_name}-க்கு மாற்றுகிறேன். தயவுசெய்து காத்திருக்கவும்."
        elif language == "hi":
            hold_speech = f"मैं आपकी कॉल {assigned_name} को ट्रांसफर कर रहा हूँ। कृपया लाइन पर बने रहें।"

        return {
            "transfer_id": transfer_session.id,
            "call_id": call_id,
            "status": "RINGING",
            "is_fallback": False,
            "assigned_agent_id": assigned_id,
            "assigned_agent_name": assigned_name,
            "message": hold_speech,
        }

    @classmethod
    async def accept_warm_transfer(
        cls,
        transfer_id: str,
        agent_id: str,
        db: AsyncSession,
        notes: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Called when Voice Agent clicks Accept.
        1. Bridges Customer <-> Voice Agent.
        2. Disconnects AI leg.
        3. Updates agent presence to ON_CALL.
        4. Customer leg remains continuous and connected.
        """
        transfer = await CallTransferRepository.get_transfer_session(transfer_id, db)
        if not transfer:
            raise ValueError(f"Transfer session '{transfer_id}' not found.")

        now_iso = _now().isoformat()
        call_obj = await db.get(Call, transfer.call_id)

        # Edge case: Customer hung up while transfer was ringing
        if call_obj and call_obj.status in ("COMPLETED", "FAILED", "NO_ANSWER", "BUSY", "CANCELED"):
            logger.warning(
                f"[CallTransfer] Accept rejected: customer already disconnected on call {transfer.call_id} (status={call_obj.status})"
            )
            await AgentAvailabilityService.set_presence(agent_id, "AVAILABLE", transfer.business_id, db)
            await CallTransferRepository.update_transfer_session_status(
                transfer_id=transfer.id,
                status="FAILED",
                failure_reason="CUSTOMER_DISCONNECTED_BEFORE_ACCEPT",
                db=db,
            )
            return {
                "success": False,
                "status": "CUSTOMER_ALREADY_HUNG_UP",
                "message": "Customer has already disconnected from the call.",
            }

        # Resolve Leg IDs and Bridge Info
        customer_leg_sid = call_obj.call_sid if call_obj else "MOCK_CUSTOMER_LEG"
        conference_name = getattr(call_obj, "conference_sid", None) or f"CONF-{transfer.call_id}"
        ai_leg_id = f"AI-LEG-{transfer.call_id}"
        agent_leg_id = f"AGENT-LEG-{agent_id}"

        logger.info(
            f"[WARM_TRANSFER_LEGS] call_id={transfer.call_id} | "
            f"customer_leg_sid={customer_leg_sid} | "
            f"conference={conference_name} | "
            f"voice_agent_id={agent_id} | "
            f"voice_agent_leg={agent_leg_id} | "
            f"ai_leg_id={ai_leg_id} | "
            f"disconnect_target={ai_leg_id} (CUSTOMER LEG PRESERVED) | "
            f"action=BRIDGING_CUSTOMER_AND_AGENT_INTO_SAME_CONFERENCE"
        )

        # Update transfer session status to ACCEPTED
        await CallTransferRepository.update_transfer_session_status(
            transfer_id=transfer.id,
            status="ACCEPTED",
            ai_disconnected=True,
            db=db
        )

        # Mark agent presence as ON_CALL
        await AgentAvailabilityService.mark_agent_on_call(
            agent_id=agent_id,
            call_id=transfer.call_id,
            business_id=transfer.business_id,
            db=db
        )

        # Update Call record: status WITH_HUMAN_AGENT & conference_sid
        if call_obj:
            call_obj.status = "WITH_HUMAN_AGENT"
            call_obj.conference_sid = conference_name
            call_obj.agent_id = agent_id
            db.add(call_obj)
            await db.commit()

            # Telephony provider bridge & stop hold music
            try:
                from app.services.hold_music_service import stop_customer_hold
                await stop_customer_hold(db, call_obj, target_agent_id=agent_id)
            except Exception as e:
                logger.warning(f"[CallTransfer] Provider bridging notice: {e}")

            # Pause AI bot so Voice Agent has exclusive bidirectional audio channel with caller
            try:
                from app.services.sip_ai_bot import pause_sip_ai_bot
                pause_sip_ai_bot(transfer.call_id)
                if call_obj:
                    pause_sip_ai_bot(call_obj.id)
                    if call_obj.call_sid:
                        pause_sip_ai_bot(call_obj.call_sid)
                        pause_sip_ai_bot(call_obj.call_sid.split("@")[0])
            except Exception as pause_err:
                logger.debug(f"[CallTransfer] AI pause notice: {pause_err}")

            # Cross-register all session aliases in barge_sessions so Voice Agent connects directly to canonical session
            try:
                from app.api.routes.barge_stream import barge_sessions, _get_or_create_session
                aliases_to_bind = set(filter(None, [
                    transfer.call_id,
                    call_obj.id if call_obj else None,
                    call_obj.call_sid if call_obj else None,
                    (call_obj.call_sid.split("@")[0] if (call_obj and call_obj.call_sid) else None),
                    transfer.id,
                ]))
                canonical_sess = None
                for k in aliases_to_bind:
                    if k in barge_sessions:
                        canonical_sess = barge_sessions[k]
                        break
                if not canonical_sess and call_obj:
                    canonical_sess = await _get_or_create_session(call_obj.id)
                if canonical_sess:
                    for k in aliases_to_bind:
                        barge_sessions[k] = canonical_sess
                        canonical_sess.setdefault("aliases", set()).add(k)
                    canonical_sess["running"] = True
                    canonical_sess["is_voice_agent_call"] = True
                    canonical_sess["status"] = "WITH_HUMAN_AGENT"
                    canonical_sess["is_3way"] = True
            except Exception as b_alias_err:
                logger.debug(f"[CallTransfer] Session alias sync notice: {b_alias_err}")

            # Flush any stale audio queues so Voice Agent 2 voice has zero latency
            try:
                from app.api.routes.barge_stream import flush_barge_session_queues_for_transfer
                flush_barge_session_queues_for_transfer(transfer.call_id)
                if call_obj.call_sid:
                    flush_barge_session_queues_for_transfer(call_obj.call_sid)
            except Exception as flush_err:
                logger.debug(f"[CallTransfer] Queue flush notice: {flush_err}")

        # Real-time WebSocket Events: transfer.accepted, ai.disconnected, and STATE_CHANGE
        accepted_event = {
            "event": "transfer.accepted",
            "type": "transfer.accepted",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "agent_id": agent_id,
            "agent_name": transfer.assigned_voice_agent_name,
            "customer_name": transfer.customer_name,
            "customer_phone": transfer.customer_phone,
            "status": "WITH_HUMAN_AGENT",
            "timestamp": now_iso,
        }

        ai_disc_event = {
            "event": "ai.disconnected",
            "type": "ai.disconnected",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "ai_leg_id": ai_leg_id,
            "message": "AI Agent disconnected from audio stream. Customer and Voice Agent are connected.",
            "timestamp": now_iso,
        }

        state_change_event = {
            "event": "STATE_CHANGE",
            "type": "STATE_CHANGE",
            "call_id": transfer.call_id,
            "state": "WITH_HUMAN_AGENT",
            "status": "WITH_HUMAN_AGENT",
            "agent_name": transfer.assigned_voice_agent_name,
            "agent_id": agent_id,
            "timestamp": now_iso,
        }

        hold_stopped_event = {
            "event": "HOLD_MUSIC_STOPPED",
            "type": "HOLD_MUSIC_STOPPED",
            "call_id": transfer.call_id,
            "call_session_id": transfer.call_id,
            "status": "HUMAN_HANDLING",
            "target_agent_id": agent_id,
            "timestamp": now_iso,
        }

        await global_ws_manager.send_to_user(agent_id, accepted_event)
        await global_ws_manager.broadcast_to_business(transfer.business_id, accepted_event)
        await global_ws_manager.broadcast_to_business(transfer.business_id, ai_disc_event)
        await global_ws_manager.broadcast_to_business(transfer.business_id, state_change_event)
        await global_ws_manager.broadcast_to_business(transfer.business_id, hold_stopped_event)
        await global_ws_manager.broadcast(accepted_event)
        await global_ws_manager.broadcast(ai_disc_event)
        await global_ws_manager.broadcast(state_change_event)
        await global_ws_manager.broadcast(hold_stopped_event)

        if getattr(transfer, "from_agent_id", None):
            is_warm = getattr(transfer, "transfer_type", "warm") in ("warm", "three_way")
            if is_warm:
                conf_event = {
                    "event": "CONFERENCE_3WAY_ACTIVE",
                    "type": "CONFERENCE_3WAY_ACTIVE",
                    "transfer_id": transfer.id,
                    "call_id": transfer.call_id,
                    "from_voice_agent_id": transfer.from_agent_id,
                    "to_voice_agent_id": agent_id,
                    "status": "CONFERENCE_3WAY_ACTIVE",
                    "is_3way": True,
                    "timestamp": now_iso,
                }
                await global_ws_manager.send_to_user(transfer.from_agent_id, conf_event)
                await global_ws_manager.broadcast_to_business(transfer.business_id, conf_event)
                await global_ws_manager.broadcast(conf_event)
            else:
                transferred_away_event = {
                    "event": "CALL_TRANSFERRED_AWAY",
                    "type": "CALL_TRANSFERRED_AWAY",
                    "transfer_id": transfer.id,
                    "call_id": transfer.call_id,
                    "from_voice_agent_id": transfer.from_agent_id,
                    "to_voice_agent_id": agent_id,
                    "status": "HUMAN_HANDLING",
                    "timestamp": now_iso,
                }
                await global_ws_manager.send_to_user(transfer.from_agent_id, transferred_away_event)
                await global_ws_manager.broadcast_to_business(transfer.business_id, transferred_away_event)
                await global_ws_manager.broadcast(transferred_away_event)

        logger.info(f"[CallTransfer] Transfer {transfer.id} accepted by agent {agent_id}. AI leg disconnected; customer leg {customer_leg_sid} continuous.")

        return {
            "success": True,
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "status": "WITH_HUMAN_AGENT",
            "assigned_agent_id": agent_id,
            "message": "Customer bridged with Voice Agent. AI leg disconnected.",
        }

    @classmethod
    async def cancel_warm_transfer(
        cls,
        transfer_id: str,
        reason: str = "CUSTOMER_HANGUP",
        db: AsyncSession = None
    ) -> Dict[str, Any]:
        """
        Called if customer hangs up while transfer is ringing.
        Releases reserved agent and marks transfer abandoned.
        """
        transfer = await CallTransferRepository.get_transfer_session(transfer_id, db)
        if not transfer:
            return {"success": False, "reason": "Not found"}

        now_iso = _now().isoformat()
        if transfer.assigned_voice_agent_id:
            await AgentAvailabilityService.release_reservation(
                agent_id=transfer.assigned_voice_agent_id,
                call_id=transfer.call_id,
                db=db,
                new_status="AVAILABLE",
            )

        await CallTransferRepository.update_transfer_session_status(
            transfer_id=transfer.id,
            status="ABANDONED",
            failure_reason=reason,
            db=db,
        )

        cancel_event = {
            "event": "transfer.cancelled",
            "type": "transfer.cancelled",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "reason": reason,
            "timestamp": now_iso,
        }
        await global_ws_manager.broadcast_to_business(transfer.business_id, cancel_event)
        await global_ws_manager.broadcast(cancel_event)

        logger.info(f"[CallTransfer] Transfer {transfer.id} cancelled. Reason: {reason}. Agent released.")
        return {"success": True, "status": "ABANDONED"}

    @classmethod
    async def decline_warm_transfer(
        cls,
        transfer_id: str,
        agent_id: str,
        reason: str,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Called when Voice Agent clicks Decline.
        Releases agent lock and attempts next available agent, or falls back to AI.
        """
        transfer = await CallTransferRepository.get_transfer_session(transfer_id, db)
        if not transfer:
            raise ValueError(f"Transfer session '{transfer_id}' not found.")

        now_iso = _now().isoformat()

        # Release current agent lock back to AVAILABLE
        await AgentAvailabilityService.release_reservation(
            agent_id=agent_id,
            call_id=transfer.call_id,
            db=db,
            new_status="AVAILABLE"
        )

        # Notify that this agent declined
        declined_event = {
            "event": "transfer.declined",
            "type": "transfer.declined",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "declined_by_agent_id": agent_id,
            "reason": reason,
            "timestamp": now_iso,
        }
        await global_ws_manager.broadcast_to_business(transfer.business_id, declined_event)

        # Try next available agent (excluding current declined agent)
        next_agent = await AgentAvailabilityService.find_and_reserve_available_agent(
            call_id=transfer.call_id,
            business_id=transfer.business_id,
            db=db,
            exclude_agent_ids=[agent_id],
            ttl_seconds=cls.DEFAULT_TIMEOUT_SECONDS,
        )

        if next_agent:
            # Re-ring next agent
            transfer.assigned_voice_agent_id = next_agent["agent_id"]
            transfer.assigned_voice_agent_name = next_agent["agent_name"]
            transfer.status = "RINGING"
            db.add(transfer)
            await db.commit()

            re_ring_event = {
                "event": "transfer.ringing",
                "type": "transfer.ringing",
                "transfer_id": transfer.id,
                "call_id": transfer.call_id,
                "customer_name": transfer.customer_name,
                "customer_phone": transfer.customer_phone,
                "target_agent_id": next_agent["agent_id"],
                "target_agent_name": next_agent["agent_name"],
                "timestamp": now_iso,
            }
            await global_ws_manager.send_to_user(next_agent["agent_id"], re_ring_event)
            await global_ws_manager.broadcast_to_business(transfer.business_id, re_ring_event)
            return {
                "success": True,
                "status": "RE_RINGING",
                "assigned_agent_name": next_agent["agent_name"],
                "message": f"Transfer redirected to {next_agent['agent_name']}.",
            }

        # No more agents -> Fall back to AI agent without dropping customer
        await CallTransferRepository.update_transfer_session_status(
            transfer_id=transfer.id,
            status="DECLINED",
            failure_reason="ALL_AGENTS_DECLINED_OR_BUSY",
            db=db
        )

        fallback_msg = "Our specialists are currently unavailable. I will arrange a prompt callback, or we can continue our conversation right now."
        fallback_event = {
            "event": "transfer.failed",
            "type": "transfer.failed",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "reason": "ALL_AGENTS_BUSY",
            "message": fallback_msg,
            "timestamp": now_iso,
        }
        await global_ws_manager.broadcast_to_business(transfer.business_id, fallback_event)
        await global_ws_manager.broadcast(fallback_event)

        return {
            "success": True,
            "status": "FALLBACK_AI",
            "message": fallback_msg,
        }

    @classmethod
    async def timeout_warm_transfer(
        cls,
        transfer_id: str,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Called when 20s ring timer expires without acceptance.
        """
        transfer = await CallTransferRepository.get_transfer_session(transfer_id, db)
        if not transfer:
            return {"success": False, "reason": "Not found"}

        timed_out_agent = transfer.assigned_voice_agent_id
        if timed_out_agent:
            await AgentAvailabilityService.release_reservation(
                agent_id=timed_out_agent,
                call_id=transfer.call_id,
                db=db,
                new_status="AVAILABLE"
            )

        # Attempt next available agent
        return await cls.decline_warm_transfer(
            transfer_id=transfer_id,
            agent_id=timed_out_agent or "unknown",
            reason="Ring timeout",
            db=db
        )

    @classmethod
    async def complete_warm_transfer(
        cls,
        transfer_id: str,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """Marks transfer completed and restores voice agent to AVAILABLE."""
        transfer = await CallTransferRepository.get_transfer_session(transfer_id, db)
        if not transfer:
            return {"success": False}

        await CallTransferRepository.update_transfer_session_status(
            transfer_id=transfer.id,
            status="COMPLETED",
            db=db
        )

        if transfer.assigned_voice_agent_id:
            await AgentAvailabilityService.set_presence(
                agent_id=transfer.assigned_voice_agent_id,
                status="AVAILABLE",
                business_id=transfer.business_id,
                db=db
            )

        comp_event = {
            "event": "transfer.completed",
            "type": "transfer.completed",
            "transfer_id": transfer.id,
            "call_id": transfer.call_id,
            "timestamp": _now().isoformat(),
        }
        await global_ws_manager.broadcast_to_business(transfer.business_id, comp_event)
        return {"success": True, "status": "COMPLETED"}
