"""
Module 11/13 — the AI engagement flow. This is the simplified-but-real
equivalent of the LangGraph `LeadEngagementGraph` described in the spec:

    load lead -> load agent -> understand message -> generate response
    -> check qualification -> check transfer condition -> (handoff)

A true LangGraph StateGraph with add_node/add_edge can be dropped in behind
`run_agent()` in ai_service.py without changing this orchestration layer —
the graph and this service are already decoupled through that one call.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy.exc import IntegrityError
from app.core.languages import LANGUAGES, get_language_name_by_code
from app.db.models import AIAgent, Conversation, KnowledgeLibrary, Lead, Message, QualificationRule
from app.services import qualification_service
from app.services.ai_service import _call_llm, run_agent
from app.services.handoff_service import build_handoff_summary, handoff_lead
from app.services.retrieval_service import RetrievalService
from app.utils.tamil_sanitizer import clean_corrupt_tamil, clean_metadata_headers, extract_spoken_dialogue
from app.services.emotion_service import analyze_voice_emotion


_conversation_fallback_cache: dict = {}
_dialogue_step_store: dict = {}
_conversation_emotion_store: dict = {}


async def _get_or_create_rule(db: AsyncSession, business_id: str) -> QualificationRule:
    rule = await db.scalar(select(QualificationRule).where(QualificationRule.business_id == business_id))
    if rule:
        return rule

    default_weights = {
        "icp": 30,
        "budget": 25,
        "timeline": 20,
        "requirement": 15,
        "decision_maker": 10,
    }

    try:
        async with db.begin_nested():
            rule = QualificationRule(
                business_id=business_id,
                weights=default_weights,
                unqualified_max=39,
                nurture_max=69,
            )
            db.add(rule)
            await db.flush()
            return rule
    except IntegrityError:
        # Another concurrent caller inserted or flushed a QualificationRule for this business.
        # The savepoint rollback ensures this session's transaction state remains valid and uncorrupted.
        rule = await db.scalar(select(QualificationRule).where(QualificationRule.business_id == business_id))
        if rule:
            return rule
        # If the sibling session's insert has not yet committed, return a safe in-memory rule instance
        # so qualification scoring completes without crashing or violating unique constraints.
        return QualificationRule(
            business_id=business_id,
            weights=default_weights,
            unqualified_max=39,
            nurture_max=69,
        )


def _extract_fields(
    lead: Lead,
    text: str,
    campaign: Any | None = None,
    extracted_criteria: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Dynamically extracts criteria and details from a customer utterance into structured customer state."""
    if not text:
        return extracted_criteria or {}

    if extracted_criteria is None:
        extracted_criteria = {}

    lowered = text.lower()

    # Extract budget/price
    if "$" in text or "₹" in text or any(k in lowered for k in ("rupee", "dollar", "usd", "inr", "lakh", "crore", "thousand", "budget", "price")):
        for token in text.split():
            if any(token.strip(".,!?").startswith(sym) for sym in ("$", "₹")) or (token.isdigit() and len(token) >= 4):
                lead.budget = token.strip(".,!?")
                extracted_criteria["budget"] = lead.budget
                break
        if "budget" not in extracted_criteria and any(k in lowered for k in ("lakh", "crore", "thousand", "usd", "inr")):
            m = re.search(r'\d+(?:\.\d+)?\s*(?:lakhs?|crores?|k|thousand|million)', lowered)
            if m:
                lead.budget = m.group(0)
                extracted_criteria["budget"] = lead.budget

    # Extract timeline or callback time
    callback_match = re.search(
        r"(?:call\s*back|callback|speak|call\s+me|follow\s*up)\s+(?:tomorrow|evening|morning|afternoon|tonight|next\s+week|at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?|[a-z]+\s+evening|[a-z]+\s+morning)",
        lowered,
    )
    if callback_match:
        callback_time = callback_match.group(0).strip()
        lead.timeline = callback_time
        extracted_criteria["timeline"] = callback_time
    elif not lead.timeline:
        for kw in ("week", "month", "asap", "quarter", "immediately", "tomorrow", "evening", "morning", "afternoon"):
            if kw in lowered:
                lead.timeline = text[:100]
                extracted_criteria["timeline"] = lead.timeline
                break

    # Dynamic entity extraction from QualificationStageEngine
    try:
        from app.services.ai.qualification_service import QualificationStageEngine
        script_text = ""
        if campaign and getattr(campaign, "schedule", None) and isinstance(campaign.schedule, dict):
            script_text = str(campaign.schedule.get("script", ""))
        extracted_entities = QualificationStageEngine.extract_entities_from_text(
            customer_text=text,
            existing_fields=extracted_criteria,
            script_text=script_text,
        )
        if extracted_entities:
            extracted_criteria.update(extracted_entities)
            if "location" in extracted_entities:
                lead.location = str(extracted_entities["location"])
            if "budget" in extracted_entities:
                lead.budget = str(extracted_entities["budget"])
            if "name" in extracted_entities and not lead.first_name:
                lead.first_name = str(extracted_entities["name"])
    except Exception as err:
        logger.debug(f"[EXTRACT_FIELDS] Dynamic entity extraction note: {err}")

    # Product/Model match
    model_match = re.search(r"\b([a-z0-9\-\_]+\s+model|model\s+[a-z0-9\-\_]+)\b", lowered)
    if model_match:
        extracted_model = model_match.group(0).title()
        extracted_criteria["product"] = extracted_model

    if not lead.requirement:
        lead.requirement = text[:500]
    elif len(lead.requirement) < 400 and text[:100] not in lead.requirement:
        lead.requirement = (lead.requirement + " " + text)[:500]

    extracted_criteria["requirement"] = lead.requirement
    return extracted_criteria


def format_prompt_for_lead(system_prompt: str, lead: Lead) -> str:
    name = f"{lead.first_name or ''} {lead.last_name or ''}".strip() or "Customer"
    location = getattr(lead, "location", "") or ""
    timeline = lead.timeline or ""
    replacements = {
        "{{name}}": name,
        "{{company}}": lead.company or "",
        "{{requirement}}": lead.requirement or "",
        "{{budget}}": lead.budget or "",
        "{{timeline}}": timeline,
        "{{location}}": location,
    }
    formatted = system_prompt
    for placeholder, val in replacements.items():
        formatted = formatted.replace(placeholder, str(val))
    return formatted



async def generate_personalized_greeting(db: AsyncSession, lead: Lead, agent: AIAgent | None) -> str:
    """Helper to dynamically construct a personalized verification introduction using the lead's details."""
    all_entries: list[KnowledgeLibrary] = []
    if agent:
        stmt_all = select(KnowledgeLibrary).where(
            KnowledgeLibrary.agent_id == agent.id,
            KnowledgeLibrary.source_data.like("Uploaded Script%")
        )
        res_all = await db.scalars(stmt_all)
        all_entries = list(res_all.all())
        
    # Sort chronological
    if all_entries:
        def get_entry_index(entry: KnowledgeLibrary) -> int:
            m = re.search(r'\d+', entry.question)
            if m:
                return int(m.group())
            return 9999
        all_entries.sort(key=get_entry_index)
        
    fname = lead.first_name or ""
    lname = lead.last_name or ""
    fullname = f"{fname} {lname}".strip()
    company = lead.company or ""
    location = lead.location or ""
    
    # Detect if RAG/script is in Tamil or Agent configured in Tamil
    is_tamil = False
    if agent and getattr(agent, "language", "") == "ta":
        is_tamil = True
    elif all_entries:
        first_ans = all_entries[0].answer or ""
        from app.services.language_service import is_tanglish_or_tamil
        if is_tanglish_or_tamil(first_ans) or re.search(r'[\u0B80-\u0BFF]', first_ans):
            is_tamil = True
            
    from app.services.language_service import get_time_of_day_salutation
    if is_tamil:
        salutation = get_time_of_day_salutation("ta")
        intro = f"{salutation}!"
        if fullname:
            intro += f" நான் {fullname} அவர்களிடம் பேசுகிறேனா?"
        else:
            intro += " நான் உங்களை தொடர்பு கொள்ளலாமா?"
        
        if company:
            intro += f" நீங்கள் {company} என்ற தொழில் செய்கிறீர்களா?"
        if location:
            intro += f" மற்றும் இதுதான் உங்கள் முகவரி: {location}, சரியா?"
            
        if all_entries:
            from app.services.ai.response_sanitizer import ResponseGuard
            raw_purpose = all_entries[0].answer or ""
            purpose = extract_spoken_dialogue(raw_purpose, is_greeting=True)
            purpose = clean_metadata_headers(purpose)
            purpose = clean_corrupt_tamil(purpose)
            guard_res = ResponseGuard.validate_customer_response(purpose)
            if purpose and len(purpose) <= 150 and guard_res["is_valid"]:
                intro += f" {purpose}"
        return intro
    else:
        salutation = get_time_of_day_salutation("en")
        intro = f"{salutation} and Thank You for Calling!"
        if fullname:
            intro += f" Am I speaking to {fullname}?"
        else:
            intro += " May I speak with you?"
        
        if company:
            intro += f" Are you running {company} business?"
        if location:
            intro += f" And is your address {location}?"
            
        if all_entries:
            from app.services.ai.response_sanitizer import ResponseGuard
            raw_purpose = all_entries[0].answer or ""
            lines = [l.strip() for l in raw_purpose.split("\n") if l.strip()]
            cleaned = []
            for line in lines:
                if any(kw in line.lower() for kw in ["rule", "instruction", "objection", "option", "version", "script", "document", "properties"]):
                    continue
                if line.endswith(":") and not re.match(r'^(?:agent|bot|assistant|[A-Za-z]{2,20})\s*:', line, re.I):
                    continue
                cleaned.append(line)
            purpose = " ".join(cleaned)
            purpose = re.sub(r'^(?:agent|bot|assistant|[A-Za-z]{2,20})\s*:\s*', '', purpose, flags=re.I)
            guard_res = ResponseGuard.validate_customer_response(purpose)
            if purpose and len(purpose) <= 150 and guard_res["is_valid"]:
                intro += f" {purpose}"
        return intro


def is_rag_required(content: str) -> bool:
    """
    Determines whether a customer utterance requires RAG / knowledge retrieval.
    Greetings, simple acknowledgments, and basic conversational responses skip RAG for zero latency.
    """
    if not content or not content.strip():
        return False

    clean = content.strip().lower().rstrip(".?!")

    simple_phrases = {
        "hi", "hello", "hey", "greetings", "good morning", "good afternoon", "good evening",
        "vanakkam", "yes", "no", "okay", "ok", "sure", "thanks", "thank you", "fine",
        "got it", "i see", "makes sense", "correct", "right", "bye", "goodbye",
        "hello / greeting / script start"
    }
    if clean in simple_phrases:
        return False

    query_keywords = (
        "price", "cost", "fee", "rate", "product", "feature", "service", "policy",
        "doctor", "available", "availability", "timing", "schedule", "specification",
        "how much", "what is", "where is", "details", "plan", "offer", "discount", "warranty",
        "பீஸ்", "கட்டணம்", "செலவு", "நேரம்", "எப்போ", "எங்கே", "யார்", "என்ன", "விவரம்",
        "மீட்டிங்", "போரம்", "டி-பிசினஸ்", "நெட்வொர்க்", "பார்வையாளர்", "பதிவு"
    )
    if any(kw in clean for kw in query_keywords):
        return True

    return RetrievalService.is_informational_query(content)


async def process_incoming_message(
    db: AsyncSession,
    lead: Lead,
    conversation: Conversation,
    agent: AIAgent | None,
    content: str,
    current_lang: str = "en",
    allowed_languages: list[str] | None = None,
    campaign: Any | None = None,
    audio_features: dict[str, Any] | None = None,
) -> dict:
    global _conversation_fallback_cache, _dialogue_step_store, _conversation_emotion_store

    lead_message = Message(
        conversation_id=conversation.id,
        sender_type="LEAD",
        sender_id=lead.id,
        content=content,
    )
    db.add(lead_message)

    extracted_criteria = getattr(conversation, "_extracted_criteria", None)
    if extracted_criteria is None:
        extracted_criteria = {}
    _extract_fields(lead, content, campaign=campaign, extracted_criteria=extracted_criteria)
    setattr(conversation, "_extracted_criteria", extracted_criteria)

    if lead.status == "NEW":
        lead.status = "ENGAGED"

    ai_message = None
    handoff_triggered = False
    detected_lang = current_lang
    is_greet_turn = False

    # Dynamic Language Detection using priority rules (Script -> Speech/Tanglish -> Session -> Fallback)
    from app.services.language_service import detect_speech_language, format_multilingual_prompt
    agent_lang = getattr(agent, "language", "en") if agent else "en"
    
    if content != "hello / greeting / script start":
        detected_lang = detect_speech_language(
            user_text=content,
            current_session_lang=current_lang,
            script_lang=agent_lang,
        )
        if allowed_languages and detected_lang not in allowed_languages:
            logger.info(f"[LANGUAGE] Detected '{detected_lang}' not in allowed campaign languages {allowed_languages}. Keeping '{current_lang}'.")
            detected_lang = current_lang
    else:
        detected_lang = agent_lang or current_lang or "en"

    logger.info(f"[CONVERSATION] Active conversation language: {detected_lang} (Previous: {current_lang})")
    lang_name = get_language_name_by_code(detected_lang)
    lead_message.meta = {"language": detected_lang, "language_name": lang_name}

    if conversation.status == "ACTIVE":
        # Selective RAG search: skip RAG database hits if query is a simple greeting or acknowledgment
        async def fetch_rag():
            rag_start = time.perf_counter()
            if not is_rag_required(content):
                logger.info(f"RAG Retrieval Skipped (Not required for turn): 0.0 ms")
                return []

            # 1. Existing KnowledgeLibrary search (legacy RAG)
            entries = await RetrievalService.search_knowledge(db, content, agent_id=agent.id if agent else None)

            # 2. FAISS Enterprise RAG augmentation — runs when business has an active RAGConfig
            if agent and agent.business_id:
                try:
                    from app.services.rag_service import RAGService
                    rag_cfg = await RAGService.get_or_create_default_rag_config(db, agent.business_id)
                    if rag_cfg:
                        faiss_chunks = await faiss_search_knowledge(
                            db=db,
                            enterprise_id=agent.business_id,
                            rag_id=rag_cfg.id,
                            query=content,
                            top_k=3
                        )
                        if faiss_chunks:
                            # Wrap FAISS results as duck-typed objects compatible with retrieved_context formatting
                            class _FaissChunkProxy:
                                def __init__(self, text): self.answer = text
                            for chunk in faiss_chunks:
                                entries.append(_FaissChunkProxy(chunk.get("text", "")))
                            logger.info(f"[FAISS RAG] Augmented with {len(faiss_chunks)} FAISS chunks for business={agent.business_id}")
                except Exception as faiss_err:
                    logger.warning(f"[FAISS RAG] Enterprise RAG augmentation skipped: {faiss_err}")

            logger.info(f"RAG Retrieval Latency: {(time.perf_counter() - rag_start) * 1000:.1f} ms")
            return entries


        async def fetch_emotions():
            emotions_start = time.perf_counter()
            if not agent:
                return []
            stmt_emotions = select(KnowledgeLibrary).where(
                KnowledgeLibrary.agent_id == agent.id,
                KnowledgeLibrary.source_data == "Uploaded Emotion Guidelines"
            )
            res_emotions = await db.scalars(stmt_emotions)
            entries = list(res_emotions.all())
            logger.info(f"Emotion Loading Latency: {(time.perf_counter() - emotions_start) * 1000:.1f} ms")
            return entries

        async def run_emotion():
            emo_start = time.perf_counter()
            prev_state = _conversation_emotion_store.get(conversation.id)
            emo_result = analyze_voice_emotion(content, audio_features, prev_state)
            _conversation_emotion_store[conversation.id] = emo_result
            logger.info(
                f"[EMOTION & TONE] Turn Latency: {(time.perf_counter() - emo_start) * 1000:.1f} ms | "
                f"Emotion: {emo_result['emotion']} (conf={emo_result['confidence']:.2f}, int={emo_result['intensity']:.2f}) -> "
                f"Tone: {emo_result['tone']}"
            )
            return emo_result

        retrieved_entries, emotion_entries, emotion_res = await asyncio.gather(
            fetch_rag(),
            fetch_emotions(),
            run_emotion(),
        )

        retrieved_context = ""
        if retrieved_entries:
            retrieved_context = "\n".join([
                f"Information: {k.answer}"
                for k in retrieved_entries
            ])

        raw_system_prompt = agent.system_prompt if agent else "You are a helpful AI sales agent."
        
        # Enforce universal multilingual hybrid system instructions for voice pipeline

        strict_instructions = (
            "\n\nCRITICAL MULTILINGUAL & HYBRID INSTRUCTIONS:\n"
            "1. UNIVERSAL MULTILINGUAL & TRANSLITERATION UNDERSTANDING:\n"
            "   - Understand user input spoken or typed in any language (English, Tamil, Hindi, Telugu, Malayalam, Kannada, Bengali, Marathi, Gujarati, Punjabi, Urdu, Arabic, Spanish, French, German, Japanese, Korean, Chinese, etc.).\n"
            "   - Understand transliterated and Romanized languages (e.g. Tanglish, Hinglish, etc.) and informal conversational variations.\n"
            "2. KNOWLEDGE ROUTING:\n"
            "   - For CallZenza or campaign/project specific questions: Use retrieved knowledge context as primary source. Do not hallucinate fake project details. If missing, state clearly that it is not available in project knowledge.\n"
            "   - For General Questions (programming, technology, science, business, education, general knowledge): Use general AI knowledge to answer directly and helpfully. Do NOT reject general questions.\n"
            "   - For Hybrid Questions: Combine general AI knowledge and project context into a single response.\n"
            "3. LANGUAGE MATCHING:\n"
            "   - Respond naturally in the user's language/script matching their spoken tone and formality."
        )
        raw_system_prompt += strict_instructions

        emotion_context = "\n".join([e.answer for e in emotion_entries]) if emotion_entries else ""

        if emotion_context:
            raw_system_prompt += f"\n\nEMOTION GUIDELINES AND INSTRUCTIONS:\n{emotion_context}\nAlways apply these emotion guidelines to your tone and responses."

        # Separate Intent vs Emotion
        clean_c = content.strip().lower()
        if clean_c in ["hello", "hi", "hey", "greetings", "good morning", "good afternoon"]:
            current_intent = "greeting"
        elif any(k in clean_c for k in ["reschedule", "appointment", "schedule"]):
            current_intent = "appointment_reschedule"
        elif any(k in clean_c for k in ["price", "cost", "fee", "how much", "rate"]):
            current_intent = "pricing_inquiry"
        elif any(pat in clean_c for pat in [
            "speak to an agent", "speak to a human", "talk to a human", "talk to an agent",
            "speak to someone", "talk to someone", "connect me", "connect to someone",
            "human representative", "transfer me", "transfer call", "speak with a person",
            "talk to a person", "human agent", "real person", "human assistance"
        ]):
            current_intent = "human_escalation_request"
        elif is_rag_required(content):
            current_intent = "knowledge_query"
        else:
            current_intent = "conversational_statement"

        emotion_meta_instruction = (
            f"\n\nSTRUCTURED EMOTION & TONE METADATA:\n"
            f"- Intent: {current_intent}\n"
            f"- Emotion: {emotion_res['emotion']} (confidence: {emotion_res['confidence']:.2f}, intensity: {emotion_res['intensity']:.2f})\n"
            f"- Target Tone: {emotion_res['tone']}\n"
            f"- LLM Tone Guidance: {emotion_res['llm_instruction']}\n"
        )
        raw_system_prompt += emotion_meta_instruction

        raw_system_prompt = format_multilingual_prompt(raw_system_prompt, detected_lang)

        system_prompt = format_prompt_for_lead(raw_system_prompt, lead)
        
        # Fetch recent pruned conversation history (last 6 messages) to maintain context without inflating latency
        history_stmt = (
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.timestamp.desc())
            .limit(6)
        )
        recent_messages = list((await db.scalars(history_stmt)).all())
        recent_messages.reverse()
        history_lines = []
        for msg in recent_messages:
            sender = "Customer" if msg.sender_type == "LEAD" else "AI Agent"
            if msg.content and msg.content.strip():
                history_lines.append(f"{sender}: {msg.content.strip()}")
        history_summary = "\n".join(history_lines) if history_lines else "No previous turns."

        prompt_parts = []
        if retrieved_context:
            prompt_parts.append(f"Retrieved Knowledge base context:\n{retrieved_context}\nUse the above information to answer accurately if related to the question.")
        else:
            prompt_parts.append("Retrieved Knowledge base context:\n(No context available)")
        
        has_campaign_script = False
        camp_script = None
        if campaign and getattr(campaign, "schedule", None) and isinstance(campaign.schedule, dict):
            camp_script = campaign.schedule.get("script")

        if camp_script and str(camp_script).strip():
            has_campaign_script = True
            agent_display = agent.name if agent and agent.name else "AI Assistant"
            org_display = campaign.name if campaign else "our organization"
            prompt_parts.append(
                f"CAMPAIGN SCRIPT & KNOWLEDGE BASE:\n{str(camp_script).strip()}\n\n"
                f"INSTRUCTIONS:\n"
                f"- You are {agent_display}, representing {org_display}.\n"
                f"- Strictly follow the conversation flow, service information, answers, and questions provided in the script above.\n"
                f"- Answer customer inquiries accurately based on this script and knowledge base.\n"
                f"- Keep your spoken response natural, polite, empathetic, and concise (1-2 sentences).\n"
                f"- Output ONLY plain natural customer-facing speech (never output XML tags like <emotion...>, <Calls Script>, or internal script labels)."
            )

        rule_obj = await _get_or_create_rule(db, lead.business_id)
        missing_criteria = qualification_service.get_missing_criteria(lead, rule_obj, extracted_criteria)

        if missing_criteria:
            missing_str = ", ".join(missing_criteria[:3])
            prompt_parts.append(f"UNFULFILLED CAMPAIGN QUALIFICATION CRITERIA TO ASK NEXT (if user didn't ask a question): {missing_str}")

        if extracted_criteria:
            known_str = ", ".join([f"{k}: {v}" for k, v in extracted_criteria.items() if v])
            prompt_parts.append(f"KNOWN CUSTOMER REQUIREMENTS SO FAR (do NOT re-ask these details): {known_str}")

        prompt_parts.append(system_prompt)
        prompt_parts.append(f"Recent Conversation History:\n{history_summary}")
        prompt_parts.append(f"Qualification focus: {agent.qualification_prompt if agent else ''}")
        prompt_parts.append(f"Lead said: {content}")
        prompt_parts.append(
            f"INSTRUCTIONS:\n"
            f"- Respond naturally in {lang_name} in 1-2 concise sentences.\n"
            f"- If the customer asked a question, answer it FIRST accurately using the retrieved knowledge context.\n"
            f"- Do NOT re-ask questions for details the lead already provided.\n"
            f"- Output ONLY spoken dialogue without metadata, chunk IDs, headers, or document labels."
        )
        
        prompt = "\n\n".join(prompt_parts)
        reply_text = None

        if not retrieved_entries and not has_campaign_script:
            clean_content = content.strip().lower().rstrip(".?!")
            greetings = ["hi", "hello", "hey", "greetings", "morning", "good morning", "good afternoon", "vanakkam"]
            if any(g in clean_content for g in greetings):
                reply_text = "வணக்கம்! வருக!" if detected_lang == "ta" else "Good morning! Welcome."
            else:
                norm_q = re.sub(r'[^\w\s]', '', content).strip().lower()
                cache_key = f"{conversation.id}:{norm_q}:{detected_lang}"
                
                if cache_key in _conversation_fallback_cache:
                    reply_text = _conversation_fallback_cache[cache_key]
                    logger.info(f"Fallback cache hit for key: {cache_key}")
                else:
                    fallback_prompt = (
                        f"The user message is: \"{content}\"\n\n"
                        f"Respond naturally in {lang_name} using your AI knowledge in 1-2 sentences.\n"
                        f"Preserve the agent's existing personality and tone."
                    )
                    try:
                        fallback_reply = await _call_llm(fallback_prompt)
                        reply_text = fallback_reply.strip()
                        _conversation_fallback_cache[cache_key] = reply_text
                        logger.info(f"Dynamic response cached under key: {cache_key}")
                    except Exception as exc:
                        logger.error(f"Gemini call failed: {exc}")
                        reply_text = "Unable to generate a response right now. Please try again."

        # Note: RAG and script entries are passed as internal prompt context above.
        # We MUST NOT assign raw RAG or raw script text directly to reply_text.
        if not reply_text:
            llm_start = time.perf_counter()
            reply_text = await run_agent(prompt)
            logger.info(f"Gemini LLM Reasoning Latency: {(time.perf_counter() - llm_start) * 1000:.1f} ms")
        
        rejection_string = "I couldn't find that information in the knowledge base."
        if reply_text and (rejection_string in reply_text or reply_text.strip() == rejection_string):
            norm_q = re.sub(r'[^\w\s]', '', content).strip().lower()
            cache_key = f"{conversation.id}:{norm_q}:{detected_lang}"

            if cache_key in _conversation_fallback_cache:
                reply_text = _conversation_fallback_cache[cache_key]
                logger.info(f"Fallback cache hit for key: {cache_key}")
            else:
                fallback_prompt = (
                    f"The user's question is not answered by the available uploaded document content.\n"
                    f"User question: \"{content}\"\n\n"
                    f"Answer the user's question naturally using your general knowledge.\n"
                    f"You MUST respond naturally in {lang_name} in 1-2 concise sentences.\n"
                    f"Preserve the agent's existing personality and tone."
                )
                try:
                    fallback_reply = await _call_llm(fallback_prompt)
                    reply_text = fallback_reply.strip()
                    _conversation_fallback_cache[cache_key] = reply_text
                    logger.info(f"Fallback response cached under key: {cache_key}")
                except Exception as exc:
                    logger.error(f"Fallback Gemini call failed: {exc}")
                    reply_text = rejection_string

        from app.services.ai.response_sanitizer import ResponseSanitizer, ResponseGuard
        raw_clean = extract_spoken_dialogue(reply_text or "", is_greeting=is_greet_turn) or ""
        clean_reply = ResponseSanitizer.clean_customer_facing_text(
            raw_clean,
            agent_name=agent.name if agent and agent.name else "Alex",
            cut_off_script_branches=True,
        )

        # Enforce hard ResponseGuard check before setting customer-facing response
        guard_result = ResponseGuard.validate_customer_response(clean_reply)
        guard_passed = guard_result["is_valid"]
        guard_reason = guard_result["reason"]

        if not guard_passed:
            logger.warning(f"[RESPONSE_GUARD] rejected_raw_context=true | reason={guard_reason} | raw_candidate=\"{clean_reply[:100]}...\"")
            regen_prompt = (
                f"The customer said: \"{content}\"\n\n"
                f"Internal context:\n{retrieved_context}\n\n"
                f"INSTRUCTIONS:\n"
                f"Provide ONLY a short 1-2 sentence polite, customer-facing spoken response in {lang_name}.\n"
                f"Do NOT output raw document titles, script labels, headers, or internal rules."
            )
            try:
                regen_reply = await _call_llm(regen_prompt)
                clean_regen = ResponseSanitizer.clean_customer_facing_text(regen_reply, agent_name=agent.name if agent else "Alex")
                regen_guard = ResponseGuard.validate_customer_response(clean_regen)
                if regen_guard["is_valid"]:
                    clean_reply = clean_regen
                    guard_passed = True
                    guard_reason = "regenerated_ok"
                else:
                    clean_reply = "We have several options available. Could you tell me what specific property or details you're looking for?"
                    guard_reason = "fallback_safe_response"
            except Exception:
                clean_reply = "We have several options available. Could you tell me what specific property or details you're looking for?"
                guard_reason = "fallback_safe_response"

        # Log turn details for tracing
        rag_preview = (retrieved_context[:100] + "...") if retrieved_context else "None"
        logger.info(
            f"\n[CALL_FLOW]\nstate=PROCESSING_AGENT\n"
            f"[STT_RESULT]\ntranscript=\"{content}\"\n"
            f"[RAG_CONTEXT]\nretrieved={bool(retrieved_entries)} length={len(retrieved_context)} preview=\"{rag_preview}\"\n"
            f"[SCRIPT_MATCH]\nmatched={bool(has_campaign_script)} intent=\"{current_intent}\"\n"
            f"[LLM_RESPONSE]\ntext=\"{clean_reply}\" length={len(clean_reply)}\n"
            f"[RESPONSE_GUARD]\npassed={guard_passed} reason=\"{guard_reason}\"\n"
            f"[TTS_INPUT]\ntext=\"{clean_reply}\" length={len(clean_reply)}"
        )

        ai_message = Message(
            conversation_id=conversation.id,
            sender_type="AI",
            sender_id=agent.id if agent else "",
            content=clean_reply,
            meta={"language": detected_lang, "language_name": lang_name}
        )
        db.add(ai_message)

        rule = await _get_or_create_rule(db, lead.business_id)
        score, status = qualification_service.score_lead(lead, rule, extracted_criteria)
        lead.score = score
        lead.qualification_status = status
        lead.ai_summary = build_handoff_summary(lead, score)

        if status == "QUALIFIED" and not lead.assigned_expert_id and (agent is None or agent.transfer_enabled):
            expert = await handoff_lead(db, lead)
            if expert:
                conversation.status = "HANDED_OFF"
                db.add(Message(
                    conversation_id=conversation.id,
                    sender_type="SYSTEM",
                    content=f"Lead qualified ({score}/100) — handed off to {expert.name}.",
                ))
                handoff_triggered = True
        elif status == "NURTURE":
            lead.status = "NURTURE" if lead.status not in ("ASSIGNED", "WON") else lead.status
        db.add(lead)

    await db.flush()
    await db.refresh(lead_message)
    if ai_message:
        await db.refresh(ai_message)
    await db.refresh(lead)

    return {
        "lead_message": lead_message,
        "ai_message": ai_message,
        "lead": lead,
        "handoff_triggered": handoff_triggered,
        "detected_lang": detected_lang,
        "intent": current_intent if conversation.status == "ACTIVE" else "closed",
        "emotion": emotion_res["emotion"] if conversation.status == "ACTIVE" else "neutral",
        "emotion_confidence": emotion_res["confidence"] if conversation.status == "ACTIVE" else 0.50,
        "emotion_intensity": emotion_res["intensity"] if conversation.status == "ACTIVE" else 0.30,
        "response_tone": emotion_res["tone"] if conversation.status == "ACTIVE" else "professional_natural",
        "speaking_rate": emotion_res["speaking_rate"] if conversation.status == "ACTIVE" else 1.0,
        "pitch": emotion_res["pitch"] if conversation.status == "ACTIVE" else "normal",
        "escalation_recommended": emotion_res.get("escalation_recommended", False) if conversation.status == "ACTIVE" else False,
        "rag_retrieved": bool(retrieved_entries) if conversation.status == "ACTIVE" else False,
        "rag_context_length": len(retrieved_context) if conversation.status == "ACTIVE" else 0,
        "rag_context_preview": (retrieved_context[:100] + "...") if (conversation.status == "ACTIVE" and retrieved_context) else "None",
        "script_matched": bool(has_campaign_script) if conversation.status == "ACTIVE" else False,
        "llm_response": clean_reply if conversation.status == "ACTIVE" else "",
        "response_guard_passed": guard_passed if conversation.status == "ACTIVE" else True,
        "response_guard_reason": guard_reason if conversation.status == "ACTIVE" else "closed",
    }
