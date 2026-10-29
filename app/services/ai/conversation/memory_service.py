"""
Conversation Memory Service.
Manages multi-turn history, session context, greeting single-fire guarantee,
dynamic intent/entity state, and explicit call lifecycle states.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional
from loguru import logger


class ConversationMemoryService:
    """In-memory session manager with structured entity tracking and lifecycle state."""

    _sessions: Dict[str, List[Dict[str, Any]]] = {}
    _state: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def _get_key(cls, agent_id: str, session_id: str) -> str:
        return f"{agent_id}:{session_id}"

    @classmethod
    def init_new_session(cls, agent_id: str, session_id: str, organization_id: Optional[str] = None) -> Dict[str, Any]:
        """Explicitly initializes or resets a session context with complete isolation."""
        cls.clear_session(agent_id, session_id)
        state = cls.get_session_state(agent_id, session_id)
        if organization_id:
            state["organization_id"] = organization_id
        return state

    @classmethod
    def get_session_state(cls, agent_id: str, session_id: str) -> Dict[str, Any]:
        """Returns the complete structured state for the session."""
        key = cls._get_key(agent_id, session_id)
        if key not in cls._state:
            cls._state[key] = {
                "agent_id": agent_id,
                "session_id": session_id,
                "language": "en",
                "greeting_sent": False,
                "call_state": "LISTENING",  # LISTENING, PROCESSING, SPEAKING, POST_RESPONSE_CHECK, WAITING_FOR_INPUT, COMPLETED
                "current_intent": None,
                "current_stage": None,
                # Script step tracking
                "current_step_index": 0,
                "side_question_step_stack": [],  # push/pop step index when handling side questions
                "pending_branch_action": None,  # e.g. "ask_callback_time"
                "collected_fields": {},
                "lead_id": None,
                "organization_id": None,
                "call_sid": None,
                "stream_sid": None,
                "campaign_id": None,
                "call_direction": "outbound",
                "created_at": time.time(),
                "last_active_at": time.time(),
            }
        return cls._state[key]

    @classmethod
    def is_greeting_sent(cls, agent_id: str, session_id: str) -> bool:
        """Returns True if initial greeting has already been delivered."""
        state = cls.get_session_state(agent_id, session_id)
        return bool(state.get("greeting_sent", False))

    @classmethod
    def mark_greeting_sent(cls, agent_id: str, session_id: str) -> None:
        """Marks initial greeting as sent (prevents duplicate greetings)."""
        state = cls.get_session_state(agent_id, session_id)
        state["greeting_sent"] = True
        state["last_active_at"] = time.time()

    @classmethod
    def set_language(cls, agent_id: str, session_id: str, language: str) -> None:
        """Updates active conversation language while preserving all other state."""
        state = cls.get_session_state(agent_id, session_id)
        state["language"] = language
        state["last_active_at"] = time.time()
        logger.info(f"[LANGUAGE] session_id={session_id} lang={language}")

    @classmethod
    def get_language(cls, agent_id: str, session_id: str, default: str = "en") -> str:
        """Returns the current language of the conversation."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("language") or default

    @classmethod
    def set_call_direction(cls, agent_id: str, session_id: str, call_direction: str) -> None:
        """Updates call direction (inbound / outbound) for this session."""
        state = cls.get_session_state(agent_id, session_id)
        state["call_direction"] = (call_direction or "outbound").lower()
        state["last_active_at"] = time.time()
        logger.info(f"[CALL_DIRECTION] session_id={session_id} direction={state['call_direction'].upper()}")

    @classmethod
    def get_call_direction(cls, agent_id: str, session_id: str, default: str = "outbound") -> str:
        """Returns the call direction (inbound / outbound) for this session."""
        state = cls.get_session_state(agent_id, session_id)
        return (state.get("call_direction") or default).lower()

    @classmethod
    def set_call_state(cls, agent_id: str, session_id: str, new_state: str) -> None:
        """Updates lifecycle state (e.g. LISTENING, SPEAKING, COMPLETED)."""
        state = cls.get_session_state(agent_id, session_id)
        state["call_state"] = new_state
        state["last_active_at"] = time.time()

    @classmethod
    def get_current_step_index(cls, agent_id: str, session_id: str) -> int:
        """Returns the current script step index (0-based) for this session."""
        state = cls.get_session_state(agent_id, session_id)
        return int(state.get("current_step_index", 0))

    @classmethod
    def set_current_step_index(cls, agent_id: str, session_id: str, index: int) -> None:
        """Updates the current script step index."""
        state = cls.get_session_state(agent_id, session_id)
        state["current_step_index"] = max(0, index)
        state["last_active_at"] = time.time()
        logger.debug(f"[Memory] Step index set to {index} for session {session_id}")

    @classmethod
    def push_side_question_state(cls, agent_id: str, session_id: str) -> None:
        """Saves the current step index before handling a side question (interrupt → answer → resume)."""
        state = cls.get_session_state(agent_id, session_id)
        stack = state.setdefault("side_question_step_stack", [])
        stack.append(int(state.get("current_step_index", 0)))
        logger.debug(f"[Memory] Pushed step {stack[-1]} to side_question_stack for session {session_id}")

    @classmethod
    def pop_side_question_state(cls, agent_id: str, session_id: str) -> Optional[int]:
        """Restores the step index after a side question is answered. Returns None if stack was empty."""
        state = cls.get_session_state(agent_id, session_id)
        stack = state.get("side_question_step_stack", [])
        if stack:
            restored = stack.pop()
            state["current_step_index"] = restored
            logger.debug(f"[Memory] Popped side_question_stack, restored step {restored} for session {session_id}")
            return restored
        return None

    @classmethod
    def set_pending_branch_action(cls, agent_id: str, session_id: str, action: Optional[str]) -> None:
        """Sets an active pending branch action (e.g. 'ask_callback_time') expecting caller answer."""
        state = cls.get_session_state(agent_id, session_id)
        state["pending_branch_action"] = action
        state["last_active_at"] = time.time()
        logger.debug(f"[Memory] Pending branch action set to {action} for session {session_id}")

    @classmethod
    def get_pending_branch_action(cls, agent_id: str, session_id: str) -> Optional[str]:
        """Returns the active pending branch action if any."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("pending_branch_action")

    @classmethod
    def get_history(cls, agent_id: str, session_id: str, limit: int = 10) -> List[Dict[str, str]]:
        """Returns the recent turns for the conversation session."""
        key = cls._get_key(agent_id, session_id)
        raw_turns = cls._sessions.get(key, [])
        formatted = []
        for t in raw_turns[-limit:]:
            formatted.append({
                "role": t.get("role", "user"),
                "content": t.get("content", ""),
            })
        return formatted

    @classmethod
    def add_turn(cls, agent_id: str, session_id: str, role: str, content: str):
        """Adds a turn to the session history and extracts any newly provided entities."""
        key = cls._get_key(agent_id, session_id)
        if key not in cls._sessions:
            cls._sessions[key] = []

        cls._sessions[key].append({
            "role": "assistant" if role in ("assistant", "model", "agent") else "user",
            "content": content.strip(),
            "timestamp": time.time(),
        })

        # Keep memory bounded to last 30 turns per session
        if len(cls._sessions[key]) > 30:
            cls._sessions[key] = cls._sessions[key][-30:]

        state = cls.get_session_state(agent_id, session_id)
        state["last_active_at"] = time.time()

        if role in ("user", "lead", "customer", "caller"):
            cls._extract_intent_and_entities(agent_id, session_id, content)

    @classmethod
    def get_entities(cls, agent_id: str, session_id: str) -> Dict[str, Any]:
        """Returns all known collected fields in this session."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("collected_fields", {})

    @classmethod
    def set_entity(cls, agent_id: str, session_id: str, field_name: str, value: Any):
        """Directly sets a collected field."""
        state = cls.get_session_state(agent_id, session_id)
        if "collected_fields" not in state:
            state["collected_fields"] = {}
        state["collected_fields"][field_name] = value
        
        # Update structured qualification state
        q_state = state.setdefault("qualification_state", {})
        q_state[field_name] = {"value": value, "completed": True}
        comp_fields = state.setdefault("completed_fields", {})
        comp_fields[field_name] = True

    @classmethod
    def get_qualification_state(cls, agent_id: str, session_id: str) -> Dict[str, Dict[str, Any]]:
        """Returns structured qualification state: {field: {'value': val, 'completed': True}}."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("qualification_state", {})

    @classmethod
    def get_completed_fields(cls, agent_id: str, session_id: str) -> Dict[str, bool]:
        """Returns map of completed field IDs."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("completed_fields", {})

    @classmethod
    def set_eligibility(cls, agent_id: str, session_id: str, status: str, reason: str = "", source: str = "SCRIPT"):
        """Sets deterministic 3-state eligibility status (PENDING, QUALIFIED, NOT_QUALIFIED)."""
        state = cls.get_session_state(agent_id, session_id)
        state["eligibility"] = {
            "status": status,
            "reason": reason,
            "source": source,
        }
        state["last_active_at"] = time.time()

    @classmethod
    def get_eligibility(cls, agent_id: str, session_id: str) -> Dict[str, Any]:
        """Returns current eligibility state."""
        state = cls.get_session_state(agent_id, session_id)
        return state.get("eligibility", {"status": "PENDING", "reason": "Initial", "source": "SCRIPT"})

    @classmethod
    def _extract_intent_and_entities(cls, agent_id: str, session_id: str, text: str):
        """
        Dynamically extracts caller intent and fields strictly from caller speech without hardcoded business assumptions.
        """
        state = cls.get_session_state(agent_id, session_id)
        collected = state.setdefault("collected_fields", {})
        clean_text = text.strip()
        lowered = clean_text.lower()

        # 1. Intent Detection
        if not state.get("current_intent"):
            if any(k in lowered for k in ["appointment", "book", "schedule", "meet", "consultation", "demo", "admission", "visit", "slot", "நேரம் ஒதுக்க"]):
                state["current_intent"] = "appointment"
                state["current_stage"] = "collect_details"
            elif any(k in lowered for k in ["price", "cost", "fee", "rate", "package", "விலை", "கட்டணம்"]):
                state["current_intent"] = "pricing_inquiry"
            elif any(k in lowered for k in ["service", "product", "provide", "offer", "சேவை"]):
                state["current_intent"] = "service_inquiry"

        # 2. Dynamic Target Entity Extraction (derived strictly from caller wording)
        if not collected.get("target_entity"):
            title_match = re.search(r'(?i)\b((?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?|prof\.?)\s+[A-Za-z]+)\b', clean_text)
            if title_match:
                candidate = title_match.group(1).strip()
                words = candidate.split()
                time_words = {"tomorrow", "today", "tonight", "morning", "afternoon", "evening", "next", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "slot", "slots"}
                if len(words) > 1 and words[-1].lower() not in time_words:
                    collected["target_entity"] = candidate
                    logger.debug(f"[Memory] Dynamically captured target_entity with title: '{collected['target_entity']}' for session {session_id}")
            if not collected.get("target_entity"):
                entity_match = re.search(
                    r'(?i)\b(?:consult\s+with|meet\s+with|speak\s+with|talk\s+to|appointment\s+with|booking\s+with)\s+(?:a|an|the)?\s*([a-zA-Z]{3,20})\b',
                    clean_text
                )
                if entity_match:
                    candidate = entity_match.group(1).strip()
                    non_entities = {
                        "appointment", "booking", "demo", "details", "info", "someone", "somebody", "anyone",
                        "tomorrow", "today", "tonight", "morning", "afternoon", "evening", "pricing", "service",
                        "with", "consult", "meet", "see", "talk", "speak", "for", "to", "at", "in", "on", "yes", "no",
                        "slot", "slots", "lump", "sum", "benefit", "benefits", "policy", "plan", "quote"
                    }
                    if candidate.lower() not in non_entities:
                        collected["target_entity"] = candidate
                        logger.debug(f"[Memory] Dynamically captured target_entity: '{candidate}' for session {session_id}")

        # 3. Dynamic Date Extraction
        if not collected.get("date"):
            date_match = re.search(
                r'(?i)\b(tomorrow|today|tonight|next\s+(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week)|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*|நாளை|இன்று)\b',
                clean_text
            )
            if date_match:
                collected["date"] = date_match.group(1).strip()
                logger.debug(f"[Memory] Captured date: '{collected['date']}' for session {session_id}")

        # 4. Dynamic Time Extraction
        time_match = re.search(
            r'(?i)\b(\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)|(?:at|after|before|around)\s+\d{1,2}(?::\d{2})?(?:\s*(?:am|pm))?|morning|afternoon|evening|noon|night|காலை|மாலை)\b',
            clean_text
        )
        if time_match:
            captured_time = time_match.group(1).strip()
            if not collected.get("time"):
                collected["time"] = captured_time
                logger.debug(f"[Memory] Captured time: '{collected['time']}' for session {session_id}")
            if state.get("pending_branch_action") in ("ask_callback_time", "schedule_callback") or not collected.get("callback_time"):
                collected["callback_time"] = captured_time
                logger.debug(f"[Memory] Captured callback_time: '{collected['callback_time']}' for session {session_id}")

        # 5. Name Extraction
        if not collected.get("name"):
            name_match = re.search(
                r'(?i)(?:my name is|i am|i\'m|this is|call me|myself|என் பெயர்)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)',
                clean_text
            )
            if name_match:
                extracted_name = name_match.group(1).strip()
                name_words = extracted_name.lower().split()
                non_names = {"interested", "looking", "calling", "here", "fine", "good", "okay", "tomorrow", "tamil", "english", "trying", "planning", "hoping", "searching", "asking", "wondering", "lump", "sum", "benefit"}
                if not any(w in non_names for w in name_words):
                    collected["name"] = extracted_name
                    logger.debug(f"[Memory] Captured name: '{extracted_name}' for session {session_id}")
            elif len(clean_text.split()) == 1 and clean_text[0].isupper() and clean_text.isalpha():
                if clean_text.lower() not in ["yes", "no", "ok", "okay", "fine", "sure", "tomorrow", "today", "tamil", "english", "hindi", "morning", "evening", "lump"]:
                    collected["name"] = clean_text
                    logger.debug(f"[Memory] Captured single-word name: '{clean_text}' for session {session_id}")

        # 6. Email Extraction
        if not collected.get("email"):
            email_match = re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', clean_text)
            if email_match:
                collected["email"] = email_match.group(0)
            else:
                spoken_email = re.search(r'\b([A-Za-z0-9._%+-]+)\s*(?:@|at)\s*([A-Za-z0-9.-]+)\s*(?:\.|dot)\s*([A-Za-z]{2,})\b', clean_text)
                if spoken_email:
                    collected["email"] = f"{spoken_email.group(1)}@{spoken_email.group(2)}.{spoken_email.group(3)}"

        # 7. Phone Extraction
        if not collected.get("phone"):
            phone_match = re.search(r'\b(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b', clean_text)
            if phone_match:
                collected["phone"] = phone_match.group(0)

        # 8. Universal Dynamic Entity Extraction via QualificationStageEngine
        from app.services.ai.qualification_service import QualificationStageEngine
        updated_entities = QualificationStageEngine.extract_entities_from_text(clean_text, existing_fields=collected)
        for k, v in updated_entities.items():
            if v is not None:
                cls.set_entity(agent_id, session_id, k, v)
        logger.debug(f"[Memory] Entities collected for session {session_id}: {collected}")

    @classmethod
    def clear_session(cls, agent_id: str, session_id: str):
        """Clears memory and state for a specific session."""
        key = cls._get_key(agent_id, session_id)
        cls._sessions.pop(key, None)
        cls._state.pop(key, None)


