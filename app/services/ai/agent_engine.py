"""
Central AI Agent Engine.
Coordinates Agent-Specific RAG, Conversation Memory, Grounded Prompting, and Gemini/Mistral LLM execution.
Serves both Voice Agent and Chatbot flows uniformly.
"""
from __future__ import annotations

import re
import time
import uuid
import asyncio
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.models import Agent, AIAgent
from app.services.ai.llm_service import get_llm_service
from app.services.ai.prompt_service import PromptService
from app.services.ai.script_engine import ScriptBranchResolver
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.ai.rag.retrieval_service import AgentRAGRetrievalService
from app.services.language_service import (
    detect_speech_language,
    detect_explicit_language_switch_request,
)

# In-memory cache for static agent metadata & parsed script (TTL 120s)
_AGENT_METADATA_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL_SECONDS = 120.0


class AgentEngineResponse:
    def __init__(
        self,
        response: str,
        agent_id: str,
        conversation_id: str,
        session_id: str,
        language: str = "en",
        retrieved_context: Optional[List[Dict[str, Any]]] = None,
        latency_ms: int = 0,
        latency_breakdown: Optional[Dict[str, int]] = None,
        emotion: str = "neutral",
        should_speak: bool = True,
        handoff: bool = False,
        end_call: bool = False,
        terminal_hangup: bool = False,
        is_dnd: bool = False,
        call_disposition: Optional[str] = None,
    ):
        self.response = response
        self.text = response
        self.response_text = response
        self.emotion = emotion
        self.should_speak = should_speak
        self.handoff = handoff
        self.end_call = end_call
        self.terminal_hangup = terminal_hangup
        self.is_dnd = is_dnd
        self.call_disposition = call_disposition
        self.agent_id = agent_id
        self.conversation_id = conversation_id
        self.session_id = session_id
        self.language = language
        self.retrieved_context = retrieved_context or []
        self.retrieved_sources = self.retrieved_context
        self.latency_ms = latency_ms
        self.latency_breakdown = latency_breakdown or {
            "stt_latency": 0,
            "intent_latency": 0,
            "rag_latency": 0,
            "gemini_latency": 0,
            "tts_latency": 0,
            "total_turn_latency": latency_ms,
        }

    @property
    def entities_captured(self) -> Dict[str, Any]:
        return ConversationMemoryService.get_entities(self.agent_id, self.session_id)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.response,
            "response": self.response,
            "response_text": self.response,
            "emotion": self.emotion,
            "should_speak": self.should_speak,
            "handoff": self.handoff,
            "end_call": self.end_call,
            "terminal_hangup": self.terminal_hangup,
            "is_dnd": self.is_dnd,
            "call_disposition": self.call_disposition,
            "agent_id": self.agent_id,
            "conversation_id": self.conversation_id,
            "session_id": self.session_id,
            "language": self.language,
            "retrieved_context": self.retrieved_context,
            "retrieved_sources": self.retrieved_sources,
            "latency_ms": self.latency_ms,
            "latency_breakdown": self.latency_breakdown,
        }



class AgentEngine:
    """Core reasoning engine powering CallZenza conversational AI agents."""

    @classmethod
    async def _get_agent_context(
        cls,
        db: AsyncSession,
        agent_id: str,
        current_language: str = "en",
        script_text: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetches agent metadata and normalized script with in-memory caching."""
        import hashlib
        script_hash = hashlib.md5((script_text or "").strip().encode("utf-8")).hexdigest()[:16]
        cache_key = f"{agent_id}:{script_hash}:{current_language}"
        now = time.time()
        cached = _AGENT_METADATA_CACHE.get(cache_key)
        if cached and (now - cached["timestamp"]) < CACHE_TTL_SECONDS:
            return cached["data"]

        agent_name = "AI Assistant"
        business_name = "our company"
        agent_persona = None
        configured_lang = current_language
        business_id = None

        if db:
            ai_agent = await db.get(AIAgent, agent_id)
            if ai_agent:
                agent_name = ai_agent.name or "AI Assistant"
                agent_persona = ai_agent.system_prompt
                configured_lang = getattr(ai_agent, "language", current_language) or current_language
                business_id = getattr(ai_agent, "business_id", None) or getattr(ai_agent, "organization_id", None)
            else:
                base_agent = await db.get(Agent, agent_id)
                if base_agent:
                    agent_name = base_agent.name or "AI Assistant"
                    agent_persona = base_agent.system_prompt
                    business_id = getattr(base_agent, "organization_id", None) or getattr(base_agent, "business_id", None)

            if business_id:
                from app.db.models import Business
                biz = await db.get(Business, business_id)
                if biz and biz.name and biz.name.strip().lower() not in {"default business", "default_business", "our company", "our team"}:
                    business_name = biz.name

        # Extract business name from system prompt ONLY if not already resolved from Business entity
        if agent_persona and (not business_name or business_name in ("our company", "our organization", "our team", "Default Business", "default business")):
            import re
            biz_match = re.search(r'(?:(?:Business|Company|Organization|Campaign)[:\s]+([A-Za-z0-9\s&\-]{2,40}))|(?:(?:calling from|on behalf of|from)\s+([A-Z][A-Za-z0-9\s&\-]{2,30}))', agent_persona, re.IGNORECASE)
            if biz_match:
                candidate_biz = (biz_match.group(1) or biz_match.group(2) or "").strip()
                if not any(w in candidate_biz.lower() for w in ["a", "an", "the", "our", "my", "this", "us", "today", "default business"]):
                    business_name = candidate_biz

        from app.services.ai.script_engine import ScriptNormalizer
        script_source = script_text or agent_persona or ""
        normalized_script = ScriptNormalizer.parse_script(
            script_text=script_source,
            agent_name=agent_name,
            business_name=business_name,
        )

        data = {
            "agent_name": agent_name,
            "business_name": business_name,
            "agent_persona": agent_persona,
            "configured_lang": configured_lang,
            "business_id": business_id,
            "script_source": script_source,
            "normalized_script": normalized_script,
        }
        _AGENT_METADATA_CACHE[cache_key] = {"data": data, "timestamp": now}
        return data

    @classmethod
    def classify_caller_intent(cls, message: str) -> str:
        user_lower = message.lower().strip()
        import re

        # 0. Do Not Call (DND) Request
        from app.services.dnd_service import detect_dnd_intent
        if detect_dnd_intent(message):
            return "dnd_request"

        # 0.5 Permission to End Call / Goodbye / That's All
        if re.search(r'\b(?:you\s+can\s+end\s+(?:the\s+)?call|end\s+(?:the\s+)?call|hang\s+up|nothing\s+else|that(?:\'s|\s+is)\s+all|that(?:\'s|\s+is)\s+it|no\s+more\s+questions?|all\s+set|bye\s*bye|bye|goodbye|no\s+thanks?\s*(?:bye)?|no\s+thank\s+you|இல்லை\s*நன்றி|வேற\s*(?:எதுவும்|ஒன்னும்)\s*இல்ல|முடிச்சுக்கலாம்|வைக்கலாம்|சரி\s*வச்சிடுங்க)\b', user_lower):
            return "permission_to_end_call"

        # 1. Explicit Language Switch
        if detect_explicit_language_switch_request(message):
            return "language_switch"

        # 1.5 Qualification Inquiry / Eligibility Check
        if re.search(r'\b(?:am\s+i\s+qualified|do\s+i\s+qualify|am\s+i\s+eligible|do\s+i\s+get|eligibility|qualification\s+status|qualification\s+criteria|will\s+i\s+qualify|qualified\s+or\s+not|தகுதியா|தகுதி)\b', user_lower):
            return "qualification_inquiry"

        # 1.6 Continuation Request
        if re.search(r'^(?:ok\s*|okay\s*|sure\s*)?(?:continue|go\s+ahead|carry\s+on|keep\s+going|next\s+step|what(?:\'s|\s+is)\s+next|tell\s+me\s+next|தொடரலாம்|அடுத்தது)[\!\.\?]*$', user_lower):
            return "continuation"

        # 1b. Human Agent Transfer Request
        from app.services.ai.script_engine import ScriptBranchResolver
        if ScriptBranchResolver._is_transfer_request(message):
            return "human_transfer_request"

        # 2. Agent Identity Confirmation ("are you an ai agent right", "are you a bot")
        if re.search(r'\b(?:are\s+you|ur|you\'re|is\s+this)\s+(?:an?\s+)?(?:ai|bot|virtual|automated)\s*(?:agent)?\b|\byou\s+(?:ur|are)\s+an?\s+ai\b', user_lower):
            return "agent_identity_confirmation"

        # 3. Agent Identity / Who is this ("who is this", "who are you", "yaar pesuradhu")
        if re.search(r'\b(?:who\s+is\s+this|who\s+are\s+you|who\s+am\s+i\s+speaking\s+with|what\s+is\s+your\s+name|yaar\s*pesuradhu|yar\s*pesura|யார்\s*பேசுறது|யார்\s*நீங்க|யார்\s*நீங்கள்)\b', user_lower):
            return "agent_identity"

        # 4. Where from / Business identity ("where are you from", "where you're from", "where are you calling from")
        if re.search(r'\b(?:where\s+(?:are\s+you|u\s+r)\s+(?:from|calling\s+from)|where\s+is\s+your\s+office|what\s+company|எந்த\s*ஊர்|எந்த\s*கம்பெனி|எந்த\s*இடம்)\b', user_lower):
            return "business_identity"

        # 5. Why did you call / Call purpose ("why did you call", "why call", "edhuku call panna", "edhyuku call pasnna", "purpose enna")
        if re.search(r'\b(?:why\s+(?:did\s+u\s+call|did\s+you\s+call|are\s+you\s+calling|call)|what\s+is\s+this\s+call\s+about|reason\s+for\s+(?:the\s+)?call|u\s+(?:only\s+)?called\s+me|you\s+(?:only\s+)?called\s+me|who\s+called\s+me|call\s+purpose|purpose\s+of\s+(?:the\s+)?call|edhuku\s*call|edhyuku\s*call|edhukku\s*call|ethukku\s*call|edhuku\s*pesuringa|edhuku\s*panna|edhuku\s*pasnna|edhyuku\s*pasnna|edhuku\s*panreenga|edhuku\s*pannirukinga|purpose\s*enna|enna\s*purpose|vishayam\s*enna|call\s*panna\s*purpose|எதுக்கு\s*call|எதற்கு\s*அழைத்தீர்கள்|காரணம்\s*என்ன|விஷயம்\s*என்ன|purpose\s*என்ன|எதுக்கு\s*கூப்பிட்டீங்க|எதுக்காக\s*call)\b', user_lower):
            return "call_purpose"

        # 5b. Appointment / Booking Request ("book an appointment", "schedule a visit", "book slot")
        if re.search(r'\b(?:appointment|book\s+(?:an?\s+)?(?:appointment|visit|slot|call)|schedule\s+(?:an?\s+)?(?:appointment|visit|call|meeting)|book|booking|consultation)\b', user_lower):
            return "appointment_request"

        # 5c. Service / Product / Feature Inquiry ("enna service", "services", "what do you do", "tell me about")
        if re.search(r'\b(?:services?|offerings?|features?|packages?|nurse\s+o\s*clock|o\s*clock|what\s+do\s+you\s+(?:do|offer|provide)|tell\s+me\s+about|what\s+is\s+this\s+about|enna\s*service|service\s*enna|enna\s*program|program\s*enna|solar\s*program|solar\s*thittam|enna\s*solreenga|என்ன\s*சேவை|என்ன\s*திட்டம்|விவரம்\s*சொல்லுங்க|விளக்குங்கள்)\b', user_lower):
            return "service_inquiry"

        # 5d. Pricing / Cost Inquiry ("how much", "price", "pricing", "cost", "rates", "fee")
        if re.search(r'\b(?:pricing|price|cost|how\s+much|rates?|fees?|charges?|quotation|estimate|விலை|கட்டணம்|எவ்வளவு)\b', user_lower):
            return "pricing_inquiry"

        # 6. Customer interest ("yes i'm interested", "interested", "tell me more")
        if re.search(r'^(?:yes\s+)?(?:i\s*am\s*|im\s*|i\'m\s*)?interested[\!\.\?]*$|\btell\s+me\s+more\b|\bsounds?\s+good\b|\bவிருப்பம்\s*உண்டு\b|\bஆர்வம்\s*உள்ளது\b', user_lower):
            return "customer_interest"

        # 7. Customer NOT interested ("i'm not interested", "dont call", "stop calling")
        if re.search(r'\b(?:not\s+interested|dont\s+call|don\'t\s+call|stop\s+calling|not\s+looking|வேண்டாம்|விருப்பமில்லை|call\s*pannாதீங்க)\b', user_lower):
            return "customer_not_interested"

        # 8. Negative response ("no", "nope", "nah", "na", "no not now", "no thanks", "no I can't")
        if re.search(r'^(?:no|nope|nah|na|no\s+not\s+now|no\s+thanks|no\s+thank\s+you|not\s+really|no\s+i\s+can\'?t|no\s+not\s+right\s+now|ill\s+pass|i\'ll\s+pass|இல்லை|வேண்டாம்)[\!\.\?]*$', user_lower) or re.search(r'^(?:no|nope|nah)\b', user_lower):
            return "negative_response"

        # 9. Positive agreement / response ("yes", "yeah", "yep", "sure", "ok", "okay", "fine", "good", "great")
        if re.search(r'^(?:yes|yeah|yep|ya|sure|ok|okay|sounds\s+good|all\s+good|ஆம்|சரி|ஆமாம்)[\!\.\?]*$', user_lower) or re.search(r'^(?:i\s*am|im|i\'m)?\s*(?:good|fine|doing\s+well|great|doing\s+good|doing\s+fine|all\s+good)[\!\.\?]*$', user_lower):
            return "positive_response"

        # 10. General greetings / Opening acknowledgement
        if re.search(r'^(?:hi|hello|hey|greetings|good\s+morning|good\s+afternoon|good\s+evening|vanakkam|வணக்கம்|namaste|नमस्ते|enna|sollu|sollunga|சொல்லுங்க|சொல்லு)[\!\.\?]*$', user_lower):
            return "greeting"

        # 11. Pleasantries / Thanks
        if re.search(r'^(?:thank\s+you|thanks|nandri|நன்றி|great\s+thanks)[\!\.\?]*$', user_lower):
            return "thank_you"

        # 12. Single unclear word (e.g. garbled STT "Maruti vicer")
        if len(user_lower.split()) <= 2 and not any(c.isalnum() for c in user_lower):
            return "unclear_speech"

        # 13. Dynamic Time / Date Answer Pattern ("5 pm", "3 pm", "tomorrow at 5", "morning", "after 6", "10:30 am", etc.)
        time_date_pattern = r'^(?:at\s+|after\s+|before\s+|around\s+)?(?:\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|\d{1,2}\s*(?:am|pm)|morning|afternoon|evening|night|noon|tomorrow(?:\s+at\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?)?|today|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday|நாளை|இன்று|காலை|மாலை)[\!\.\?]*$'
        if re.search(time_date_pattern, user_lower):
            return "information_provided"

        # 14. Email Address Pattern
        if re.search(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}[\!\.\?]*$', user_lower):
            return "information_provided"

        return "general_query"

    @classmethod
    async def process_user_query(
        cls,
        db: AsyncSession,
        agent_id: str,
        message: str,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        is_voice: bool = False,
        current_language: str = "en",
        llm_provider: Optional[str] = None,
        stt_latency_ms: int = 0,
        tts_latency_ms: int = 0,
        script_text: Optional[str] = None,
        call_direction: Optional[str] = None,
    ) -> AgentEngineResponse:
        """
        Executes the complete grounded AI Agent pipeline:
        Query -> Language Detection -> Intent Detection -> Agent & Business Context ->
        Agent RAG Retrieval (if required) -> Conversation Memory -> Gemini LLM -> Grounded Response.
        """
        start_time = time.perf_counter()
        intent_start = time.perf_counter()
        clean_msg = message.strip() if message else ""

        # Step 1: Ensure active session and conversation IDs
        active_session_id = session_id or conversation_id or str(uuid.uuid4())
        active_conv_id = conversation_id or active_session_id

        # Log direct input to AgentEngine
        logger.info(f"[AGENT_ENGINE_INPUT] call_id={active_session_id} session_id={active_session_id} transcript='{clean_msg}'")

        # Resolve dynamic call direction
        effective_direction = (
            call_direction
            or ConversationMemoryService.get_call_direction(agent_id, active_session_id)
            or "outbound"
        ).lower()
        logger.info(f"[CALL_DIRECTION] session_id={active_session_id} direction={effective_direction.upper()}")

        # Step 1.5: Validate STT transcript for empty, noise, hallucination, or garbled input
        from app.services.ai.transcript_validator import TranscriptValidator
        history_turns = ConversationMemoryService.get_history(agent_id, active_session_id, limit=2)
        prev_user_msg = next((t["content"] for t in reversed(history_turns) if t.get("role") == "user"), None)

        session_lang = ConversationMemoryService.get_language(agent_id, active_session_id, default=current_language)
        val_res = TranscriptValidator.validate_transcript(
            transcript=clean_msg,
            language=session_lang,
            previous_transcript=prev_user_msg,
        )

        if not val_res["is_valid"]:
            intent_lat = int((time.perf_counter() - intent_start) * 1000)
            total_lat = int((time.perf_counter() - start_time) * 1000)
            repeat_msg = val_res["repeat_prompt"] or "I'm sorry, I didn't catch that. Could you please repeat that?"
            logger.info(f"[AgentEngine] Invalid input intercepted: {val_res['reason']} | Prompting repeat in {session_lang}")
            return AgentEngineResponse(
                response=repeat_msg,
                agent_id=agent_id,
                conversation_id=active_conv_id,
                session_id=active_session_id,
                language=session_lang,
                latency_ms=total_lat,
                latency_breakdown={
                    "stt_latency": stt_latency_ms,
                    "intent_latency": intent_lat,
                    "rag_latency": 0,
                    "gemini_latency": 0,
                    "tts_latency": tts_latency_ms,
                    "total_turn_latency": total_lat,
                },
            )

        # Step 2: Fetch Cached Agent Persona, Business Identity, and Normalized Script
        ctx = await cls._get_agent_context(
            db=db,
            agent_id=agent_id,
            current_language=current_language,
            script_text=script_text,
        )
        agent_name = ctx["agent_name"]
        business_name = ctx["business_name"]
        agent_persona = ctx["agent_persona"]
        configured_lang = ctx["configured_lang"]
        business_id = ctx["business_id"]
        script_source = ctx["script_source"]
        normalized_script = ctx["normalized_script"]
        active_script_id = getattr(normalized_script, "id", None) or getattr(normalized_script, "campaign_id", None) or "default_script"

        logger.info(
            f"[SESSION_CONTEXT] session_id={active_session_id} campaign_id={active_script_id} "
            f"organization_id={business_id or 'default'} organization_name='{business_name}' "
            f"agent_id={agent_id} script_id={active_script_id} rag_id={business_id or 'default'} "
            f"call_direction={effective_direction}"
        )

        # Check if user is asking a question or inquiry
        is_user_asking = "?" in clean_msg or any(
            w in clean_msg.lower() for w in [
                "what", "how", "tell", "details", "price", "pricing", "where", "when",
                "can you", "who", "which", "service", "services", "book", "appointment",
                "about", "nurse", "am i", "do i", "qualify", "qualified", "eligible",
                "கட்டணம்", "என்ன", "எப்படி", "எப்போது", "விவரம்", "தகுதியா", "தகுதி"
            ]
        )

        # Step 2.5: Extract Dynamic Entities & Update Qualification State
        from app.services.ai.qualification_service import QualificationStageEngine
        existing_entities = ConversationMemoryService.get_entities(agent_id, active_session_id)
        script_raw = normalized_script.raw_text or script_source or ""
        
        # Dynamic Stage Parsing
        dynamic_stages = QualificationStageEngine.extract_stages_from_script(
            script_text=script_raw,
            agent_name=agent_name,
            business_name=business_name,
        )
        current_step_idx = ConversationMemoryService.get_current_step_index(agent_id, active_session_id)
        active_stage_meta = dynamic_stages[current_step_idx] if 0 <= current_step_idx < len(dynamic_stages) else (dynamic_stages[0] if dynamic_stages else None)
        expected_field = active_stage_meta.get("field") if active_stage_meta else None
        
        # Semantic Entity Extraction
        newly_extracted = QualificationStageEngine.extract_entities_from_text(
            clean_msg,
            existing_fields=existing_entities,
            script_text=script_raw,
        )
        for k, v in newly_extracted.items():
            if v is not None:
                ConversationMemoryService.set_entity(agent_id, active_session_id, k, v)
        known_entities = ConversationMemoryService.get_entities(agent_id, active_session_id)
        completed_fields_map = ConversationMemoryService.get_completed_fields(agent_id, active_session_id)

        # Dynamic Stage Progress & Eligibility Evaluation
        updated_stages, current_stage_name, active_stage_idx, qual_status, missing_fields, next_field = QualificationStageEngine.evaluate_progress(
            stages=dynamic_stages,
            collected_fields=known_entities,
            current_step_idx=current_step_idx,
            script_text=script_raw,
            rag_context="",
        )
        ConversationMemoryService.set_eligibility(
            agent_id,
            active_session_id,
            status=qual_status,
            reason="Evaluated from active Script/RAG",
            source="SCRIPT",
        )

        # Step 3: Classify Caller Intent & Resolve Script Action
        from app.services.ai.script_engine import ScriptBranchResolver

        detected_intent = cls.classify_caller_intent(clean_msg)
        if newly_extracted and any(v is not None for v in newly_extracted.values()) and detected_intent in ("general_query", "positive_response"):
            detected_intent = "information_provided"

        # Advance current_step_idx if qualification requirements are met and this is not a side-question
        if qual_status == "PENDING" and active_stage_idx is not None and not is_user_asking and detected_intent != "qualification_inquiry":
            current_step_idx = active_stage_idx
            ConversationMemoryService.set_current_step_index(agent_id, active_session_id, active_stage_idx)

        # Structured Telemetry Logs
        logger.info(f'[CONVERSATION_INPUT] "{clean_msg}"')
        logger.info(f'[CURRENT_SCRIPT] script_id="{active_script_id}" stages_count={len(dynamic_stages)}')
        logger.info(f'[CURRENT_STAGE] "{active_stage_meta.get("name") if active_stage_meta else current_stage_name}"')
        logger.info(f'[EXPECTED_FIELD] "{expected_field or "none"}"')
        logger.info(f'[QUALIFICATION_EXTRACT] {newly_extracted if newly_extracted else "{}"}')
        logger.info(f'[QUALIFICATION_STATE] {known_entities if known_entities else "{}"}')
        logger.info(f'[COMPLETED_FIELDS] {list(completed_fields_map.keys())}')
        logger.info(f'[INTENT_DETECTED] session_id={active_session_id} intent="{detected_intent}"')
        logger.info(f'[QUALIFICATION_STATUS] {qual_status}')
        logger.info(f'[NEXT_MISSING_FIELD] field="{next_field or "none"}"')
        logger.info(f'[STAGE_TRANSITION] from="{active_stage_meta.get("name") if active_stage_meta else "Initial"}" to="{current_stage_name}"')

        if detected_intent in ("qualification_inquiry", "eligibility_inquiry"):
            logger.info(f'[SIDEBAR_QUESTION] session_id={active_session_id} type="qualification_status"')

        # Step 3.5: Dynamic Language Detection & Tracking
        session_lang = ConversationMemoryService.get_language(agent_id, active_session_id, default=configured_lang)
        if detected_intent == "language_switch" or detect_explicit_language_switch_request(clean_msg):
            explicit_switch = detect_explicit_language_switch_request(clean_msg)
            target_lang = explicit_switch[0] if explicit_switch else "ta"
            old_lang = ConversationMemoryService.get_language(agent_id, active_session_id, default=current_language)
            ConversationMemoryService.set_language(agent_id, active_session_id, target_lang)
            detected_intent = "language_switch"
            detected_lang = target_lang
            logger.info(
                f"[LANGUAGE_SWITCH] session_id={active_session_id} old_language={old_lang} "
                f"new_language={target_lang} current_stage='{current_stage_name}' "
                f"call_direction={effective_direction.upper()}"
            )
        else:
            detected_lang = detect_speech_language(
                user_text=clean_msg,
                current_session_lang=session_lang,
                script_lang=configured_lang,
            )
            ConversationMemoryService.set_language(agent_id, active_session_id, detected_lang)

        state = ConversationMemoryService.get_session_state(agent_id, active_session_id)
        history = ConversationMemoryService.get_history(agent_id, active_session_id, limit=6)
        recent_bot_msg = None
        for h in reversed(history):
            if h.get("role") in ("assistant", "agent", "model"):
                recent_bot_msg = h.get("content")
                break

        # Step 4: Special Lifecycle Routing (DND, Call Termination Permission, Qualification Confirmation)
        from app.services.dnd_service import detect_dnd_intent
        if detected_intent == "dnd_request" or detect_dnd_intent(clean_msg):
            logger.info(f"[DND_TRIGGERED] Caller requested DND in session {active_session_id}: '{clean_msg}'")
            if detected_lang in ("ta", "tamil"):
                dnd_reply = "மன்னிக்கவும். உங்கள் எண்ணை எங்கள் அழைப்புத் தவிர்ப்புப் பட்டியலில் சேர்த்துள்ளோம். இனி உங்களுக்கு எங்கள் நிறுவனத்திலிருந்து அழைப்புகள் வராது. உங்கள் நேரத்திற்கு நன்றி!"
            else:
                dnd_reply = "I apologize for any inconvenience. I have added your number to our Do Not Call list and you will not receive any further calls from us. Thank you for your time."

            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=dnd_reply)
            intent_lat = int((time.perf_counter() - intent_start) * 1000)
            total_lat = int((time.perf_counter() - start_time) * 1000)
            breakdown = {
                "stt_latency": stt_latency_ms,
                "intent_latency": intent_lat,
                "rag_latency": 0,
                "gemini_latency": 0,
                "tts_latency": tts_latency_ms,
                "total_turn_latency": total_lat,
            }
            return AgentEngineResponse(
                response=dnd_reply,
                agent_id=agent_id,
                conversation_id=active_conv_id,
                session_id=active_session_id,
                language=detected_lang,
                retrieved_context=[],
                latency_ms=total_lat,
                latency_breakdown=breakdown,
                end_call=True,
                terminal_hangup=True,
                is_dnd=True,
                call_disposition="DNC",
            )

        # Check if caller requested to end call, or is responding to: "Is there anything else I can help you with, or may I end the call?"
        is_direct_end_call = (detected_intent == "permission_to_end_call")
        if state.get("end_call_permission_asked") or is_direct_end_call:
            user_said_yes_to_end = (
                detected_intent in ("positive_response", "permission_to_end_call", "negative_response", "thank_you")
                and not is_user_asking
                and not any(w in clean_msg.lower() for w in ["wait", "more", "question", "one more", "what about", "tell me", "details", "help"])
            )
            if user_said_yes_to_end or is_direct_end_call:
                logger.info(f"[CALL_TERMINATION_PERMISSION_GRANTED] Caller requested/granted permission to end call in session {active_session_id}")
                if detected_lang in ("ta", "tamil"):
                    bye_reply = "மிக்க நன்றி! நல்ல நாளாக அமையட்டும். விடைபெறுகிறேன்."
                else:
                    bye_reply = "Thank you so much for your time. Have a wonderful day ahead! Goodbye."

                ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
                ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=bye_reply)
                intent_lat = int((time.perf_counter() - intent_start) * 1000)
                total_lat = int((time.perf_counter() - start_time) * 1000)
                breakdown = {
                    "stt_latency": stt_latency_ms,
                    "intent_latency": intent_lat,
                    "rag_latency": 0,
                    "gemini_latency": 0,
                    "tts_latency": tts_latency_ms,
                    "total_turn_latency": total_lat,
                }
                if state.get("service_confirmed") is False:
                    final_disp = "NOT_INTERESTED"
                elif qual_status == "QUALIFIED" and state.get("service_confirmed"):
                    final_disp = "QUALIFIED"
                elif qual_status == "NOT_QUALIFIED":
                    final_disp = "NOT_QUALIFIED"
                else:
                    final_disp = "COMPLETED"

                return AgentEngineResponse(
                    response=bye_reply,
                    agent_id=agent_id,
                    conversation_id=active_conv_id,
                    session_id=active_session_id,
                    language=detected_lang,
                    retrieved_context=[],
                    latency_ms=total_lat,
                    latency_breakdown=breakdown,
                    end_call=True,
                    terminal_hangup=True,
                    call_disposition=final_disp,
                )
            else:
                # Caller wants to ask or clarify more questions before ending call
                logger.info(f"[CALL_TERMINATION_PERMISSION_HELD] Caller asked follow-up question: '{clean_msg}'")
                state["end_call_permission_asked"] = False

        # Step 4.2: Qualification Lifecycle (Confirmation & Permission Request)
        if qual_status == "QUALIFIED":
            if not state.get("service_confirmation_asked"):
                logger.info(f"[SERVICE_CONFIRMATION_TRIGGERED] Qualification satisfied for session {active_session_id}. Prompting service confirmation.")
                state["service_confirmation_asked"] = True
                if detected_lang in ("ta", "tamil"):
                    confirm_reply = f"சிறப்பு! நீங்கள் {business_name} திட்டத்திற்கு வெற்றிகரமாக தகுதி பெற்றுள்ளீர்கள். உங்கள் விவரங்களின்படி எங்களின் நிபுணர் விரைவில் உங்களை தொடர்புகொள்வார். இந்த பதிவை உங்களுக்கு உறுதி செய்யலாமா?"
                else:
                    confirm_reply = f"Great news! You are fully qualified for the {business_name} program based on your details. Would you like me to confirm this booking for you?"

                ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
                ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=confirm_reply)
                intent_lat = int((time.perf_counter() - intent_start) * 1000)
                total_lat = int((time.perf_counter() - start_time) * 1000)
                breakdown = {
                    "stt_latency": stt_latency_ms,
                    "intent_latency": intent_lat,
                    "rag_latency": 0,
                    "gemini_latency": 0,
                    "tts_latency": tts_latency_ms,
                    "total_turn_latency": total_lat,
                }
                return AgentEngineResponse(
                    response=confirm_reply,
                    agent_id=agent_id,
                    conversation_id=active_conv_id,
                    session_id=active_session_id,
                    language=detected_lang,
                    retrieved_context=[],
                    latency_ms=total_lat,
                    latency_breakdown=breakdown,
                    call_disposition="QUALIFIED",
                )
            elif not state.get("end_call_permission_asked"):
                # Customer responds to service confirmation
                is_declining = detected_intent in ("negative_response", "customer_not_interested") or any(
                    w in clean_msg.lower() for w in ["no", "not now", "don't want", "dont want", "not interested", "ill pass", "no thanks", "வேண்டாம்", "இல்லை", "nahi", "mat"]
                )
                if is_declining:
                    logger.info(f"[SERVICE_CONFIRMATION_DECLINED] Customer declined service in session {active_session_id}")
                    state["service_confirmed"] = False
                    state["end_call_permission_asked"] = True
                    if detected_lang in ("ta", "tamil"):
                        permission_reply = "புரிந்துகொண்டேன். இந்த பதிவை நீங்கள் தற்போது தொடர விரும்பவில்லை என்பதை குறித்துக்கொண்டேன். உங்கள் நேரத்திற்கு நன்றி! நான் உங்களுக்கு வேறு ஏதாவது உதவ முடியுமா, அல்லது அழைப்பை நிறைவு செய்யலாமா?"
                    else:
                        permission_reply = "Understood. I have noted that you would not like to proceed with this booking at this time. Thank you for your time! Is there anything else I can assist you with, or may I end the call?"

                    ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
                    ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=permission_reply)
                    intent_lat = int((time.perf_counter() - intent_start) * 1000)
                    total_lat = int((time.perf_counter() - start_time) * 1000)
                    breakdown = {
                        "stt_latency": stt_latency_ms,
                        "intent_latency": intent_lat,
                        "rag_latency": 0,
                        "gemini_latency": 0,
                        "tts_latency": tts_latency_ms,
                        "total_turn_latency": total_lat,
                    }
                    return AgentEngineResponse(
                        response=permission_reply,
                        agent_id=agent_id,
                        conversation_id=active_conv_id,
                        session_id=active_session_id,
                        language=detected_lang,
                        retrieved_context=[],
                        latency_ms=total_lat,
                        latency_breakdown=breakdown,
                        call_disposition="NOT_INTERESTED",
                    )

                has_actual_question = "?" in clean_msg or any(w in clean_msg.lower() for w in ["what", "how", "when", "where", "why", "who", "which", "how much", "cost", "price", "tell me"])
                if not has_actual_question:
                    logger.info(f"[PERMISSION_BEFORE_HANGUP_TRIGGERED] Service confirmed in session {active_session_id}. Asking permission before hanging up.")
                    state["service_confirmed"] = True
                    state["end_call_permission_asked"] = True
                    if detected_lang in ("ta", "tamil"):
                        permission_reply = "நன்றி, உங்கள் விவரங்கள் வெற்றிகரமாக பதிவு செய்யப்பட்டன! நான் உங்களுக்கு வேறு ஏதாவது உதவ முடியுமா, அல்லது அழைப்பை நிறைவு செய்யலாமா?"
                    else:
                        permission_reply = "Perfect, your details and booking are confirmed! Is there anything else I can help you with today, or may I end the call?"

                    ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
                    ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=permission_reply)
                    intent_lat = int((time.perf_counter() - intent_start) * 1000)
                    total_lat = int((time.perf_counter() - start_time) * 1000)
                    breakdown = {
                        "stt_latency": stt_latency_ms,
                        "intent_latency": intent_lat,
                        "rag_latency": 0,
                        "gemini_latency": 0,
                        "tts_latency": tts_latency_ms,
                        "total_turn_latency": total_lat,
                    }
                    return AgentEngineResponse(
                        response=permission_reply,
                        agent_id=agent_id,
                        conversation_id=active_conv_id,
                        session_id=active_session_id,
                        language=detected_lang,
                        retrieved_context=[],
                        latency_ms=total_lat,
                        latency_breakdown=breakdown,
                        call_disposition="QUALIFIED",
                    )

        # Step 4.3: Script-Aware Action & Branch Resolution
        # If DISQUALIFIED: route immediately to non-qualified flow and STOP asking qualification questions
        if qual_status == "NOT_QUALIFIED":
            clean_close = normalized_script.closing_message
            if any(w in clean_close.lower() for w in ["you qualify", "eligible", "book you", "schedule", "appointment"]):
                clean_close = "Thank you for your time! Have a wonderful day."

            if detected_lang in ("ta", "tamil"):
                non_qual_resp = "மன்னிக்கவும், எங்கள் திட்டம் தற்போது வீட்டு உரிமையாளர்களுக்கு மட்டுமே பொருந்தும். உங்கள் நேரத்திற்கு நன்றி, நல்ல நாளாக அமையட்டும்!"
            else:
                non_qual_resp = f"Understood. Unfortunately, our program is currently only available for homeowners. {clean_close}"
            
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=non_qual_resp)
            intent_lat = int((time.perf_counter() - intent_start) * 1000)
            total_lat = int((time.perf_counter() - start_time) * 1000)
            breakdown = {
                "stt_latency": stt_latency_ms,
                "intent_latency": intent_lat,
                "rag_latency": 0,
                "gemini_latency": 0,
                "tts_latency": tts_latency_ms,
                "total_turn_latency": total_lat,
            }
            return AgentEngineResponse(
                response=non_qual_resp,
                agent_id=agent_id,
                conversation_id=active_conv_id,
                session_id=active_session_id,
                language=detected_lang,
                retrieved_context=[],
                latency_ms=total_lat,
                latency_breakdown=breakdown,
                end_call=True,
                terminal_hangup=True,
                call_disposition="NOT_QUALIFIED",
            )
        else:
            resolved_action = ScriptBranchResolver.resolve_action(
                script=normalized_script,
                current_step_index=current_step_idx,
                intent=detected_intent,
                collected_entities=known_entities or ConversationMemoryService.get_entities(agent_id, active_session_id),
                caller_msg=clean_msg,
                recent_bot_msg=recent_bot_msg,
                pending_branch_action=state.get("pending_branch_action"),
            )
            logger.info(f'[NEXT_ACTION] "{resolved_action.get("action")}"')

        # Track or clear pending branch action
        if resolved_action.get("action") == "ask_callback_time":
            ConversationMemoryService.set_pending_branch_action(agent_id, active_session_id, "ask_callback_time")
        elif resolved_action.get("advance_step"):
            ConversationMemoryService.set_pending_branch_action(agent_id, active_session_id, None)

        # Fast path: Transfer to human
        if resolved_action.get("action") == "transfer_to_human":
            reply_text = resolved_action.get("response_text", "I'd be glad to connect you with a representative. Please hold while I transfer your call.")
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=reply_text)
            intent_lat = int((time.perf_counter() - intent_start) * 1000)
            total_lat = int((time.perf_counter() - start_time) * 1000)
            breakdown = {
                "stt_latency": stt_latency_ms,
                "intent_latency": intent_lat,
                "rag_latency": 0,
                "gemini_latency": 0,
                "tts_latency": 0,
                "total_turn_latency": total_lat,
            }
            return AgentEngineResponse(
                response=reply_text,
                agent_id=agent_id,
                conversation_id=active_conv_id,
                session_id=active_session_id,
                language=configured_lang,
                retrieved_context=[],
                latency_ms=total_lat,
                latency_breakdown=breakdown,
                handoff=True,
            )

        # ── Side-question stack management ────────────────────────────────────
        if resolved_action.get("action") in ("knowledge_lookup", "handle_unknown_objection", "answer_qualification_inquiry"):
            ConversationMemoryService.push_side_question_state(agent_id, active_session_id)
        elif not resolved_action.get("requires_rag") and not resolved_action.get("action", "").startswith("handle"):
            popped = ConversationMemoryService.pop_side_question_state(agent_id, active_session_id)
            if popped is not None:
                current_step_idx = popped

        if resolved_action.get("advance_step"):
            if qual_status == "PENDING" and active_stage_idx is not None:
                new_step_idx = active_stage_idx
            else:
                new_step_idx = resolved_action.get("target_step_index", current_step_idx)
            ConversationMemoryService.set_current_step_index(agent_id, active_session_id, new_step_idx)
            current_step_idx = new_step_idx

        if is_user_asking:
            resolved_action["requires_rag"] = True

        # Step 5: Fast-Path for Script-Resolved Direct Answers with Strict Echo Guard
        if "response_text" in resolved_action and not resolved_action.get("requires_rag") and not is_user_asking:
            from app.services.ai.response_sanitizer import ResponseSanitizer
            raw_fast = resolved_action["response_text"]
            fast_emotion, _ = ResponseSanitizer.extract_emotion_and_text(raw_fast)
            fast_reply = ResponseSanitizer.clean_customer_facing_text(
                raw_fast,
                agent_name=normalized_script.agent_name,
                cut_off_script_branches=True,
            )
            # Response Guard against echo or direct customer transcript repetition
            is_echo = (
                fast_reply.strip().lower() == clean_msg.strip().lower()
                or not fast_reply.strip()
                or len(fast_reply.strip()) < 3
            )
            if is_echo:
                logger.info(
                    f"[RESPONSE_GUARD] session_id={active_session_id} customer_text='{clean_msg}' "
                    f"candidate_response='{raw_fast}' reason='Echo/repetition detected - routing to LLM'"
                )
            else:
                ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
                ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=fast_reply)
                intent_lat = int((time.perf_counter() - intent_start) * 1000)
                total_lat = int((time.perf_counter() - start_time) * 1000)
                breakdown = {
                    "stt_latency": stt_latency_ms,
                    "intent_latency": intent_lat,
                    "rag_latency": 0,
                    "gemini_latency": 0,
                    "tts_latency": tts_latency_ms,
                    "total_turn_latency": total_lat,
                }
                logger.info(f"[RESPONSE_SOURCE] SCRIPT")
                logger.info(f"[SCRIPT_MATCH] session_id={active_session_id} matched_section='{resolved_action.get('action')}'")
                return AgentEngineResponse(
                    response=fast_reply,
                    agent_id=agent_id,
                    conversation_id=active_conv_id,
                    session_id=active_session_id,
                    language=detected_lang,
                    retrieved_context=[],
                    latency_ms=total_lat,
                    latency_breakdown=breakdown,
                    emotion=fast_emotion,
                )

        # Record User Turn
        ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
        intent_lat = int((time.perf_counter() - intent_start) * 1000)

        # Step 7: Load Conversation Memory & Known Entities
        history = ConversationMemoryService.get_history(agent_id, active_session_id, limit=8)

        # Step 8: Perform Agent RAG Retrieval
        retrieved_chunks = []
        rag_lat = 0
        if is_user_asking or resolved_action.get("requires_rag") or detected_intent in ("service_inquiry", "pricing_inquiry", "appointment_request", "general_query"):
            rag_start = time.perf_counter()
            retrieved_chunks = await AgentRAGRetrievalService.retrieve_agent_knowledge(
                db=db,
                agent_id=agent_id,
                query=clean_msg,
                business_id=business_id,
                top_k=3,
                threshold=0.15,
            )
            rag_lat = int((time.perf_counter() - rag_start) * 1000)

        has_context = len(retrieved_chunks) > 0
        context_str = "\n\n".join([
            f"- {c.get('text', '')}" for c in retrieved_chunks if c.get("text")
        ])

        logger.info(f"[RAG_CONTEXT] session_id={active_session_id} query='{clean_msg}' chunks={len(retrieved_chunks)}")

        # Step 9: Build Grounded System Prompt with Dynamic Qualification Guidance
        active_step = normalized_script.get_step_by_index(current_step_idx)
        # Ensure active_step points to an uncollected question if the current step is already satisfied
        if qual_status == "NOT_QUALIFIED":
            active_step = normalized_script.steps[-1] if normalized_script.steps else None
        elif active_step and active_step.required_field and active_step.required_field in known_entities and known_entities[active_step.required_field] is not None:
            for s in normalized_script.steps[current_step_idx:]:
                field_key = s.required_field or QualificationStageEngine._infer_field_key(s.title)
                if field_key not in known_entities or known_entities[field_key] is None:
                    active_step = s
                    break
        elif not active_step and normalized_script.steps:
            for s in normalized_script.steps:
                field_key = s.required_field or QualificationStageEngine._infer_field_key(s.title)
                if field_key not in known_entities or known_entities[field_key] is None:
                    active_step = s
                    break

        active_step_title = active_step.title if active_step else "In Progress"
        active_step_instructions = active_step.raw_instructions if active_step else ""

        system_prompt = PromptService.build_system_prompt(
            agent_name=normalized_script.agent_name,
            business_name=normalized_script.business_name,
            agent_persona=agent_persona,
            retrieved_context=context_str,
            known_entities=known_entities,
            detected_language=detected_lang,
            detected_intent=detected_intent,
            current_step=active_step_title,
            call_direction=effective_direction,
            has_relevant_context=has_context,
            script_full_text=normalized_script.raw_text or script_source,
            step_instructions=active_step_instructions,
            script_action_instruction=resolved_action.get("prompt_instruction", ""),
            qualification_state=known_entities,
            missing_fields=missing_fields,
            current_stage=current_stage_name,
            next_field=next_field,
        )

        # Step 10: Invoke Primary Live LLM (Gemini)
        turn_num = (len(history) // 2) + 1
        llm = get_llm_service(llm_provider)
        model_tag = getattr(llm, "default_model", "gemini-3.1-flash-lite")
        logger.info(f"[LLM_REQUEST] model={model_tag} session_id={active_session_id} prompt='{clean_msg}'")

        gemini_start = time.perf_counter()
        raw_response = ""
        source_tag = "gemini"
        try:
            raw_response = await llm.generate_response(
                prompt=clean_msg,
                system_prompt=system_prompt,
                history=history,
                temperature=0.1,
                max_tokens=85,
            )
            gemini_lat = int((time.perf_counter() - gemini_start) * 1000)
        except Exception as exc:
            source_tag = "fallback"
            gemini_lat = int((time.perf_counter() - gemini_start) * 1000)
            logger.error(f"[GEMINI_ERROR] error='{exc}'")
            if qual_status == "NOT_QUALIFIED":
                raw_response = non_qual_resp
            elif detected_intent == "call_purpose":
                if detected_lang in ("ta", "tamil"):
                    raw_response = f"நான் {normalized_script.business_name} நிறுவனத்திலிருந்து {normalized_script.outbound_purpose} தொடர்பாக அழைக்கிறேன். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    raw_response = f"I am {normalized_script.agent_name} calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent == "service_inquiry":
                if detected_lang in ("ta", "tamil"):
                    raw_response = f"நாங்கள் {normalized_script.business_name} மூலம் சிறந்த சோலார் சேவைகளை வழங்குகிறோம். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    raw_response = f"We provide solar energy solutions through {normalized_script.business_name}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent == "greeting":
                if detected_lang in ("ta", "tamil"):
                    raw_response = f"வணக்கம்! நான் {normalized_script.business_name} சார்பாக {normalized_script.outbound_purpose} பற்றி பேச அழைக்கிறேன். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    raw_response = f"Hello! I am calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif has_context and retrieved_chunks:
                raw_response = f"{retrieved_chunks[0].get('text', '')}"
            elif active_step and active_step.question_to_ask:
                raw_response = active_step.question_to_ask
            elif qual_status == "QUALIFIED":
                raw_response = normalized_script.closing_message
            else:
                raw_response = f"I am {normalized_script.agent_name} calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}."

        # Step 11: Clean Response & Validate Response matches Script Action
        from app.services.ai.response_sanitizer import ResponseSanitizer
        detected_emotion, _ = ResponseSanitizer.extract_emotion_and_text(raw_response)
        clean_response = ResponseSanitizer.clean_customer_facing_text(
            raw_response,
            agent_name=normalized_script.agent_name,
            cut_off_script_branches=True,
        )

        # Anti-Echo and Empty Validation Guard
        echo_detected = bool(clean_response.strip().lower() == clean_msg.strip().lower() or not clean_response.strip())
        wrong_direction = bool(
            effective_direction == "outbound"
            and any(k in clean_response.lower() for k in ["எப்படி உதவலாம்", "how can i help you", "how may i help", "how may i assist", "help you today"])
        )
        language_mismatch = False
        if detected_lang in ("ta", "tamil") and not any(0x0B80 <= ord(c) <= 0x0BFF for c in clean_response) and len(clean_response.split()) > 3:
            language_mismatch = True

        # Generic duplicate-question guard: check all already-collected entities
        duplicate_question = False
        _resp_lower = clean_response.lower()
        _FIELD_QUESTION_SIGNALS: Dict[str, List[str]] = {
            "home_ownership": ["own or rent", "சொந்த வீடா", "வாடகை வீடா", "own your home", "homeowner", "rent or own", "do you own"],
            "property_type": ["single family", "தனி வீடா", "அபார்ட்மெண்ட்", "apartment or house", "type of home", "type of property"],
            "tax_payer": ["taxpayer", "pay tax", "pay taxes", "வரி", "வரி செலுத்"],
            "name": ["your name", "உங்கள் பெயர்", "may i know your name", "can i get your name", "what is your name"],
            "phone": ["phone number", "contact number", "mobile number", "உங்கள் தொலைபேசி", "உங்கள் மொபைல்"],
            "email": ["email address", "your email", "மின்னஞ்சல்"],
            "zip_code": ["zip code", "postal code", "pin code", "அஞ்சல் குறியீடு", "பின் கோடு"],
            "date": ["preferred date", "appointment date", "which date", "எந்த தேதி", "விரும்பும் தேதி"],
            "time": ["preferred time", "appointment time", "which time", "எந்த நேரம்", "விரும்பும் நேரம்"],
            "location": ["your location", "your city", "உங்கள் இடம்", "உங்கள் நகரம்"],
            "budget": ["your budget", "price range", "உங்கள் பட்ஜெட்"],
        }
        for _field, _signals in _FIELD_QUESTION_SIGNALS.items():
            if _field in known_entities and known_entities[_field] is not None:
                if any(sig in _resp_lower for sig in _signals):
                    duplicate_question = True
                    break

        # Qualification Status Guard
        qualification_claim_invalid = False
        if qual_status == "NOT_QUALIFIED":
            # If AI falsely claims they qualify or continues to ask qualification questions
            if any(w in _resp_lower for w in ["you qualify", "you are eligible", "you're eligible", "you are qualified"]):
                qualification_claim_invalid = True
                logger.warning(f"[RESPONSE_GUARD] Blocked false qualification claim for NOT_QUALIFIED status.")
            elif any(w in _resp_lower for w in ["single family", "apartment", "zip code", "electricity bill", "property type"]):
                qualification_claim_invalid = True
                logger.warning(f"[RESPONSE_GUARD] Blocked subsequent qualification question after disqualification.")
        elif qual_status == "QUALIFIED":
            if any(w in _resp_lower for w in ["not qualified", "not eligible", "don't qualify"]):
                qualification_claim_invalid = True
                logger.warning(f"[RESPONSE_GUARD] Blocked false disqualification claim for QUALIFIED status.")
        elif qual_status == "PENDING":
            if any(w in _resp_lower for w in ["you qualify", "you are qualified", "you're qualified", "you are eligible", "you're eligible", "qualify for the", "eligible for the"]):
                qualification_claim_invalid = True
                logger.warning(f"[RESPONSE_GUARD] Blocked premature qualification claim for PENDING status.")

        # Check for generic fallback repetition or prompt instructions
        generic_fallback_detected = bool(
            any(phrase in clean_response.lower() for phrase in [
                "share more details", "share a few more details", "so i can assist you",
                "how can i help you today", "how may i help", "எப்படி உதவலாம்",
                "sure, let's continue", "let's continue with", "you must speak solely",
                "understood. let me check the remaining details with you",
            ])
        )

        _needs_correction = (
            echo_detected
            or (wrong_direction and len(history) > 1)
            or language_mismatch
            or duplicate_question
            or generic_fallback_detected
            or qualification_claim_invalid
        )
        if _needs_correction:
            _reason = (
                "qualification_claim_invalid" if qualification_claim_invalid
                else "echo" if echo_detected
                else "wrong_direction" if wrong_direction
                else "language_mismatch" if language_mismatch
                else "duplicate_question" if duplicate_question
                else "generic_fallback"
            )
            logger.warning(
                f"[RESPONSE_GUARD_CORRECTION] session_id={active_session_id} customer_text='{clean_msg}' "
                f"candidate_response='{clean_response}' reason='{_reason}'"
            )
            if qual_status == "NOT_QUALIFIED":
                clean_response = non_qual_resp
            elif detected_intent == "agent_identity":
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"நான் {normalized_script.business_name} சார்பாக பேசும் {normalized_script.agent_name}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    clean_response = f"I am {normalized_script.agent_name} calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent == "call_purpose":
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"ஆம், நான் {normalized_script.business_name} நிறுவனத்திலிருந்து {normalized_script.outbound_purpose} தொடர்பாக அழைக்கிறேன். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    clean_response = f"Yes, I'm calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent == "service_inquiry":
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"நாங்கள் {normalized_script.business_name} மூலம் சிறந்த சோலார் தீர்வுகளை வழங்குகிறோம். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    clean_response = f"We provide solar energy solutions through {normalized_script.business_name}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent == "greeting":
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"வணக்கம்! நான் {normalized_script.business_name} சார்பாக {normalized_script.outbound_purpose} பற்றி பேச அழைக்கிறேன். {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    clean_response = f"Hello! I am calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif detected_intent in ("qualification_inquiry", "eligibility_inquiry"):
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"தகுதி என்பது உங்கள் மற்ற விவரங்களைப் பொறுத்தது. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
                else:
                    clean_response = f"Qualification depends on a few remaining details. {active_step.question_to_ask if active_step and active_step.question_to_ask else ''}".strip()
            elif duplicate_question and active_step and active_step.question_to_ask:
                clean_response = active_step.question_to_ask
            elif has_context and retrieved_chunks and not duplicate_question:
                clean_response = retrieved_chunks[0].get("text", "")
                if active_step and active_step.question_to_ask and not any(sig in clean_response.lower() for sig in _FIELD_QUESTION_SIGNALS.get(active_step.required_field or "", [])):
                    clean_response = f"{clean_response} {active_step.question_to_ask}".strip()
            elif active_step and active_step.question_to_ask:
                if detected_intent in ("positive_response", "continuation"):
                    clean_response = active_step.question_to_ask
                else:
                    clean_response = active_step.question_to_ask
            elif qual_status == "QUALIFIED":
                clean_response = normalized_script.closing_message
            else:
                logger.info(
                    f"[IN_PROGRESS_CONTINUE] script_id={active_script_id} stage='{current_stage_name}' "
                    f"expected_field='{expected_field}' remaining={missing_fields}"
                )
                if detected_lang in ("ta", "tamil"):
                    clean_response = f"நான் {normalized_script.business_name} நிறுவனத்திலிருந்து {normalized_script.outbound_purpose} தொடர்பாக அழைக்கிறேன்."
                else:
                    clean_response = f"I am {normalized_script.agent_name} calling from {normalized_script.business_name} regarding {normalized_script.outbound_purpose}."

        logger.info(
            f"[RESPONSE_GUARD] session_id={active_session_id} status=\"{'PASS' if not _needs_correction else 'CORRECTED'}\" "
            f"qual_status={qual_status} duplicate_question={duplicate_question} "
            f"generic_fallback={generic_fallback_detected} echo_detected={echo_detected}"
        )

        if qual_status == "PENDING" and next_field and active_step and active_step.question_to_ask:
            logger.info(f"[NEXT_QUESTION] session_id={active_session_id} field='{next_field}' question='{active_step.question_to_ask}' reason='Next uncollected script requirement'")

        logger.info(f"[GEMINI_RESPONSE] assistant_response='{clean_response}'")

        # Cross-campaign company name leak guard: verify response does not mention another company
        if business_name and business_name.lower() not in ("our company", "our organization", "our team"):
            if "home solar experts" in clean_response.lower() and "home solar experts" not in business_name.lower():
                logger.warning(
                    f"[RESPONSE_GUARD] Blocked cross-campaign company leak in response. "
                    f"session_id={active_session_id} replaced_with='{business_name}'"
                )
                clean_response = re.sub(r'(?i)\bhome\s+solar\s+experts\b', business_name, clean_response)

        action_name = resolved_action.get("action", "")
        if action_name == "ask_callback_time":
            callback_keywords = ["time", "call", "when", "later", "schedule", "reach", "convenient", "talk", "நாளை", "நேரம்", "கூப்பிட"]
            if not any(w in clean_response.lower() for w in callback_keywords):
                logger.warning(f"[AgentEngine] LLM response '{clean_response}' didn't mention callback time for ask_callback_time. Enforcing script action.")
                if detected_lang in ("ta", "tamil"):
                    clean_response = "சரி, உங்களுக்கு எப்போது கூப்பிட வசதியாக இருக்கும்?"
                else:
                    clean_response = "Understood. When would be a convenient time for me to call you back?"

        ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=clean_response)

        logger.info(
            f"[RESPONSE_SOURCE] organization_source='{business_name}' script_source='{active_script_id}' "
            f"rag_source='{business_id or 'default'}' response_source='{source_tag}'"
        )

        total_lat = int((time.perf_counter() - start_time) * 1000)
        breakdown = {
            "stt_latency": stt_latency_ms,
            "intent_latency": intent_lat,
            "rag_latency": rag_lat,
            "gemini_latency": gemini_lat,
            "tts_latency": tts_latency_ms,
            "total_turn_latency": total_lat,
        }
        logger.info(
            f"[VOICE_LATENCY] stt_ms={stt_latency_ms} llm_first_token_ms=0 llm_total_ms={gemini_lat} "
            f"tts_first_byte_ms=0 tts_total_ms={tts_latency_ms} total_response_ms={total_lat} | "
            f"Intent: {intent_lat}ms | RAG: {rag_lat}ms | LLM: {gemini_lat}ms | TTS: {tts_latency_ms}ms | "
            f"Agent: '{agent_id}' | Lang: {detected_lang} | Emotion: {detected_emotion}"
        )

        return AgentEngineResponse(
            response=clean_response,
            agent_id=agent_id,
            conversation_id=active_conv_id,
            session_id=active_session_id,
            language=detected_lang,
            retrieved_context=retrieved_chunks,
            latency_ms=total_lat,
            latency_breakdown=breakdown,
            emotion=detected_emotion,
        )


    @classmethod
    async def stream_user_query(
        cls,
        db: AsyncSession,
        agent_id: str,
        message: str,
        conversation_id: Optional[str] = None,
        session_id: Optional[str] = None,
        is_voice: bool = False,
        current_language: str = "en",
        llm_provider: Optional[str] = None,
        stt_latency_ms: int = 0,
        script_text: Optional[str] = None,
        call_direction: Optional[str] = None,
    ) -> AsyncIterator[Tuple[str, bool]]:
        """
        Streaming generator for low-latency Voice response pipelining.
        Yields (sentence_chunk, is_final) so the audio synthesis (TTS) worker
        can begin playing the first sentence immediately while remaining tokens are generated.
        """
        start_time = time.perf_counter()
        clean_msg = message.strip() if message else ""
        active_session_id = session_id or conversation_id or str(uuid.uuid4())
        active_conv_id = conversation_id or active_session_id

        effective_direction = (
            call_direction
            or ConversationMemoryService.get_call_direction(agent_id, active_session_id)
            or "outbound"
        ).lower()

        if not clean_msg:
            yield "Please provide a question or message.", True
            return

        ctx = await cls._get_agent_context(
            db=db,
            agent_id=agent_id,
            current_language=current_language,
            script_text=script_text,
        )
        agent_name = ctx["agent_name"]
        business_name = ctx["business_name"]
        agent_persona = ctx["agent_persona"]
        configured_lang = ctx["configured_lang"]
        business_id = ctx["business_id"]
        script_source = ctx["script_source"]
        normalized_script = ctx["normalized_script"]

        # Entity Extraction & Qualification State Evaluation
        from app.services.ai.qualification_service import QualificationStageEngine
        existing_entities = ConversationMemoryService.get_entities(agent_id, active_session_id)
        newly_extracted = QualificationStageEngine.extract_entities_from_text(
            clean_msg,
            existing_fields=existing_entities,
            script_text=normalized_script.raw_text or script_source,
        )
        for k, v in newly_extracted.items():
            if v is not None:
                ConversationMemoryService.set_entity(agent_id, active_session_id, k, v)
        known_entities = ConversationMemoryService.get_entities(agent_id, active_session_id)

        # Dynamic Stage Parsing & Progress Evaluation
        script_raw = normalized_script.raw_text or script_source or ""
        dynamic_stages = QualificationStageEngine.extract_stages_from_script(
            script_text=script_raw,
            agent_name=agent_name,
            business_name=business_name,
        )
        current_step_idx = ConversationMemoryService.get_current_step_index(agent_id, active_session_id)
        updated_stages, current_stage_name, active_stage_idx, qual_status, missing_fields, next_field = QualificationStageEngine.evaluate_progress(
            stages=dynamic_stages,
            collected_fields=known_entities,
            current_step_idx=current_step_idx,
        )

        detected_intent = cls.classify_caller_intent(clean_msg)
        # Handle Explicit Language Switch State Update (Preserves all session state)
        if detected_intent == "language_switch" or detect_explicit_language_switch_request(clean_msg):
            explicit_switch = detect_explicit_language_switch_request(clean_msg)
            target_lang = explicit_switch[0] if explicit_switch else "ta"
            old_lang = ConversationMemoryService.get_language(agent_id, active_session_id, default=current_language)
            ConversationMemoryService.set_language(agent_id, active_session_id, target_lang)
            detected_intent = "language_switch"
            logger.info(
                f"[LANGUAGE_SWITCH] session_id={active_session_id} old_language={old_lang} "
                f"new_language={target_lang} current_stage='{current_stage_name}' "
                f"call_direction={effective_direction.upper()}"
            )

        state = ConversationMemoryService.get_session_state(agent_id, active_session_id)
        current_step_idx = ConversationMemoryService.get_current_step_index(agent_id, active_session_id)
        history = ConversationMemoryService.get_history(agent_id, active_session_id, limit=6)
        recent_bot_msg = None
        for h in reversed(history):
            if h.get("role") in ("assistant", "agent", "model"):
                recent_bot_msg = h.get("content")
                break

        resolved_action = ScriptBranchResolver.resolve_action(
            script=normalized_script,
            current_step_index=current_step_idx,
            intent=detected_intent,
            collected_entities=known_entities,
            caller_msg=clean_msg,
            recent_bot_msg=recent_bot_msg,
            pending_branch_action=state.get("pending_branch_action"),
        )

        if resolved_action.get("action") == "ask_callback_time":
            ConversationMemoryService.set_pending_branch_action(agent_id, active_session_id, "ask_callback_time")
        elif resolved_action.get("advance_step"):
            ConversationMemoryService.set_pending_branch_action(agent_id, active_session_id, None)

        if resolved_action.get("advance_step") and "target_step_index" in resolved_action:
            new_step_idx = resolved_action["target_step_index"]
            ConversationMemoryService.set_current_step_index(agent_id, active_session_id, new_step_idx)

        # Fast path 0: Transfer to human
        if resolved_action.get("action") == "transfer_to_human":
            reply_text = resolved_action.get("response_text", "I'd be glad to connect you with a representative. Please hold while I transfer your call.")
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=reply_text)
            yield reply_text, True
            return

        # Fast path 1: Language Switch
        if detected_intent == "language_switch":
            explicit_switch = detect_explicit_language_switch_request(clean_msg)
            target_lang = explicit_switch[0] if explicit_switch else "ta"
            ack_message = explicit_switch[1] if explicit_switch else "சரி, இனிமேல் நான் தமிழில் பேசுகிறேன். எப்படி உதவலாம்?"
            ConversationMemoryService.set_language(agent_id, active_session_id, target_lang)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=ack_message)
            yield ack_message, True
            return

        # Fast path 2: Direct Script Action
        if "response_text" in resolved_action and not resolved_action.get("requires_rag"):
            fast_reply = resolved_action["response_text"]
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=fast_reply)
            yield fast_reply, True
            return

        # IMPORTANT: If explicit language switch was committed above, preserve it.
        session_lang = ConversationMemoryService.get_language(agent_id, active_session_id, default=configured_lang)
        if detected_intent == "language_switch":
            detected_lang = session_lang  # already committed by the explicit switch block
        else:
            detected_lang = detect_speech_language(
                user_text=clean_msg,
                current_session_lang=session_lang,
                script_lang=configured_lang,
            )
            ConversationMemoryService.set_language(agent_id, active_session_id, detected_lang)
        ConversationMemoryService.add_turn(agent_id, active_session_id, role="user", content=clean_msg)

        history = ConversationMemoryService.get_history(agent_id, active_session_id, limit=8)

        retrieved_chunks = []
        if resolved_action.get("requires_rag"):
            retrieved_chunks = await AgentRAGRetrievalService.retrieve_agent_knowledge(
                db=db,
                agent_id=agent_id,
                query=clean_msg,
                business_id=business_id,
                top_k=3,
                threshold=0.15,
            )

        has_context = len(retrieved_chunks) > 0
        context_str = "\n\n".join([f"- {c.get('text', '')}" for c in retrieved_chunks if c.get("text")])

        active_step = normalized_script.get_step_by_index(
            ConversationMemoryService.get_current_step_index(agent_id, active_session_id)
        )
        active_step_title = active_step.title if active_step else "In Progress"
        active_step_instructions = active_step.raw_instructions if active_step else ""

        system_prompt = PromptService.build_system_prompt(
            agent_name=normalized_script.agent_name,
            business_name=normalized_script.business_name,
            agent_persona=agent_persona,
            retrieved_context=context_str,
            known_entities=known_entities,
            detected_language=detected_lang,
            detected_intent=detected_intent,
            current_step=active_step_title,
            call_direction=effective_direction,
            has_relevant_context=has_context,
            script_full_text=normalized_script.raw_text or script_source,
            step_instructions=active_step_instructions,
            script_action_instruction=resolved_action.get("prompt_instruction", ""),
            qualification_state=known_entities,
            missing_fields=missing_fields,
            current_stage=current_stage_name,
            next_field=next_field,
        )

        llm = get_llm_service(llm_provider)
        sentence_buffer = ""
        full_response = ""
        first_token_time: Optional[float] = None
        sentence_delimiters = (". ", "! ", "? ", "\n", "। ", ".\n", "!\n", "?\n")

        try:
            async for token in llm.generate_response_stream(
                prompt=clean_msg,
                system_prompt=system_prompt,
                history=history,
                temperature=0.2,
                max_tokens=256,
            ):
                if first_token_time is None:
                    first_token_time = time.perf_counter()
                    ttft_ms = int((first_token_time - start_time) * 1000)
                    logger.debug(f"[VOICE_LATENCY] Time-To-First-Token: {ttft_ms}ms")

                sentence_buffer += token
                full_response += token

                # Check if buffer forms a complete sentence
                for delim in sentence_delimiters:
                    if delim in sentence_buffer:
                        parts = sentence_buffer.split(delim, 1)
                        complete_sentence = parts[0].strip()
                        if complete_sentence:
                            clean_sentence = PromptService.clean_response(complete_sentence)
                            if clean_sentence:
                                yield clean_sentence, False
                        sentence_buffer = parts[1]
                        break

            # Yield remaining buffer
            if sentence_buffer.strip():
                clean_sentence = PromptService.clean_response(sentence_buffer.strip())
                if clean_sentence:
                    yield clean_sentence, True

            final_clean = PromptService.clean_response(full_response)
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=final_clean)
            stream_dur_ms = int((time.perf_counter() - start_time) * 1000)
            ttft_val = int((first_token_time - start_time) * 1000) if first_token_time else 0
            logger.info(
                f"[VOICE_LATENCY] stt_ms={stt_latency_ms} llm_first_token_ms={ttft_val} llm_total_ms={stream_dur_ms} "
                f"tts_first_byte_ms=0 tts_total_ms=0 total_response_ms={stream_dur_ms} | "
                f"Stream generation completed for agent '{agent_id}'"
            )

        except Exception as exc:
            logger.error(f"[AgentEngine Stream] Streaming error: {exc}")
            fallback = active_step.question_to_ask if (active_step and active_step.question_to_ask) else "I'd be glad to help with that. Could you share a few more details?"
            yield fallback, True
            ConversationMemoryService.add_turn(agent_id, active_session_id, role="assistant", content=fallback)

    # Convenience alias for chat
    chat = process_user_query

