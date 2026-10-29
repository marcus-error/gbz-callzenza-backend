"""
Prompt Construction and Grounding Rules Service.
Enforces strict grounding, anti-hallucination, scope enforcement, and dynamic multilingual responses.
Delivers the full script context and step-specific instructions to Gemini in a structured format.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional


class PromptService:
    """Handles system prompt construction, context formatting, and response cleaning."""

    @staticmethod
    def build_system_prompt(
        agent_name: str = "AI Assistant",
        business_name: str = "our company",
        agent_persona: Optional[str] = None,
        retrieved_context: str = "",
        known_entities: Optional[Dict[str, Any]] = None,
        detected_language: str = "en",
        detected_intent: Optional[str] = None,
        current_step: Optional[str] = None,
        call_direction: str = "outbound",
        has_relevant_context: bool = True,
        # Script grounding context
        script_full_text: Optional[str] = None,
        step_instructions: Optional[str] = None,
        script_action_instruction: Optional[str] = None,
        # Dynamic qualification state
        qualification_state: Optional[Dict[str, Any]] = None,
        missing_fields: Optional[List[str]] = None,
        current_stage: Optional[str] = None,
        next_field: Optional[str] = None,
    ) -> str:
        """
        Builds a comprehensive system prompt enforcing all grounding and conversation rules.
        Delivers the active script text and dynamic qualification state so Gemini follows the script,
        never asks for already collected information, and uses zero hardcoded industry assumptions.
        """
        persona = (
            agent_persona.strip()
            if agent_persona and agent_persona.strip()
            else f"You are {agent_name}, an AI agent calling from {business_name}."
        )

        # ── Dynamic Qualification & Known entities block ─────────────────────
        qualification_section = ""
        all_collected: Dict[str, Any] = {}
        if known_entities:
            all_collected.update(known_entities)
        if qualification_state:
            all_collected.update({k: v for k, v in qualification_state.items() if v is not None})

        if all_collected:
            entity_lines = [f"- {k.replace('_', ' ').title()}: {v}" for k, v in all_collected.items() if v]
            if entity_lines:
                qualification_section += (
                    "\nINFORMATION ALREADY COLLECTED FROM CUSTOMER (STRICT RULE: NEVER ASK FOR THESE AGAIN):\n"
                    + "\n".join(entity_lines)
                    + "\n"
                )

        if missing_fields:
            clean_missing = [f.replace('_', ' ').title() for f in missing_fields if f]
            if clean_missing:
                qualification_section += f"REMAINING REQUIRED FIELDS TO COLLECT: {', '.join(clean_missing)}\n"
        if next_field:
            qualification_section += f"NEXT REQUIRED FIELD ACCORDING TO SCRIPT: {next_field.replace('_', ' ').title()}\n"
        if current_stage:
            qualification_section += f"CURRENT QUALIFICATION STAGE: {current_stage}\n"

        # ── RAG knowledge context ────────────────────────────────────────────
        context_section = ""
        if retrieved_context and retrieved_context.strip():
            context_section = f"""
ASSIGNED AGENT KNOWLEDGE (use to answer customer's specific question):
{retrieved_context.strip()}
"""
        else:
            context_section = """
ASSIGNED AGENT KNOWLEDGE:
(No additional knowledge retrieval needed for this turn — follow the Agent Script instructions below.)
"""

        # ── Full Agent Script context for script grounding ────────────────────
        script_section = ""
        if script_full_text and script_full_text.strip():
            truncated_script = script_full_text.strip()[:1400]
            script_section = f"""
ASSIGNED AGENT SCRIPT (your source of truth for the conversation flow):
{truncated_script}
{'...[script continues]' if len(script_full_text.strip()) > 1400 else ''}
"""

        # ── Active step instructions ─────────────────────────────────────────
        step_section = ""
        if step_instructions and step_instructions.strip():
            step_section = f"""
CURRENT SCRIPT STEP INSTRUCTIONS:
{step_instructions.strip()[:600]}
"""

        # ── Script action decided by engine ──────────────────────────────────
        action_section = ""
        if script_action_instruction and script_action_instruction.strip():
            action_section = f"""
SPECIFIC NEXT ACTION DECIDED BY SCRIPT ENGINE:
{script_action_instruction.strip()}
"""

        # ── Intent guidance ──────────────────────────────────────────────────
        intent_guidance = ""
        if detected_intent:
            intent_guidance = (
                f"\nDETECTED CALLER INTENT: {detected_intent}\n"
                f"CURRENT CONVERSATION STEP: {current_step or 'In Progress'}\n"
            )
            if detected_intent == "language_switch":
                intent_guidance += (
                    f"- The customer requested to speak in {detected_language.upper()}.\n"
                    f"- Acknowledge the language transition naturally and briefly in {detected_language.upper()} (e.g., 'சரி, தமிழில் தொடரலாம்.' if Tamil or 'Sure, continuing in English.' or 'जी हाँ, हिंदी में जारी रखते हैं।').\n"
                    f"- IMMEDIATELY continue the conversation by asking or addressing the CURRENT conversation step ({current_step or 'In Progress'}) or next uncollected requirement in {detected_language.upper()}.\n"
                    f"- STRICT RULE: Do NOT restart the conversation, do NOT introduce yourself or the company again if already introduced, and do NOT say 'எப்படி உதவலாம்?' or 'How can I help you?'. Continue directly from where the call was.\n"
                )
            elif detected_intent == "negative_response":
                intent_guidance += (
                    "- The caller gave a negative response. "
                    "Follow the SPECIFIC NEXT ACTION instruction directly according to the assigned script.\n"
                )
            elif detected_intent == "positive_response":
                intent_guidance += (
                    "- The caller responded positively. "
                    "Transition naturally to the next required script field or action.\n"
                )
            elif detected_intent == "customer_interest":
                intent_guidance += (
                    "- The caller expressed interest. "
                    "Ask the next uncollected script requirement as indicated in the script.\n"
                )
            elif detected_intent == "customer_not_interested":
                intent_guidance += (
                    "- The caller expressed lack of interest. "
                    "Acknowledge politely and close the call according to the script.\n"
                )

        system_instruction = f"""You are the AI voice agent for the current organization.

You MUST answer the customer's current question using the provided CURRENT AGENT SCRIPT/RAG CONTEXT.

The Script/RAG context is the primary source of truth for business-specific information.

Do not invent business information.
Do not invent services, prices, timings, policies, locations, names, or availability.

If the answer is available in the provided context, answer naturally and concisely.

If the answer is not available in the provided context, use the fallback behavior defined by the current agent script.

Do not repeat the customer's question as your answer.

Keep responses short, conversational, and natural for a live phone conversation (1-2 sentences max).

{persona}

CRITICAL VOICE & SCRIPT GROUNDING RULES:

1. RECEPTIONIST VOICE & SPEED:
   - You are a fast, natural AI voice agent for {business_name}.
   - Call Direction: {call_direction.upper()}
   - Answer the customer's latest question directly and concisely (1 to 2 short sentences).
   - STRICT RULE: Do NOT ask for information that the customer already provided.
   - For OUTBOUND calls: Do NOT say "How can I help you today?" or "How may I assist you?" or "எப்படி உதவலாம்?". You initiated the call, so follow the outbound script.
   - DO NOT USE MARKDOWN FORMATTING (no asterisks **, *, __, _, #, bullet points) in voice replies. Speak plain conversational text.

2. SCRIPT COMPLIANCE & KNOWLEDGE GROUNDING:
   - The assigned Agent Script and ASSIGNED AGENT KNOWLEDGE are your primary sources of truth.
   - If the answer exists in Script/RAG, answer it directly.
   - If clarification is genuinely required, ask one concise clarification for the remaining missing fields.
   - Do not use generic fallback responses when relevant Script/RAG information exists.
   - Do not invent business information or locations.

3. MULTILINGUAL & LANGUAGE PERSISTENCE:
   - Speak naturally in {detected_language.upper()}.
   - If the customer switches languages, acknowledge immediately with a brief transition phrase and continue smoothly in {detected_language.upper()} while preserving all collected facts and current stage.
   - Never randomly switch back to English if the user has chosen another language.
   - Never restart the conversation, repeat introductions, or ask "How can I help you?" when switching languages.
   - Output ONLY the spoken words. Do NOT include instructions, prompt rules, or meta-commentary in your response.
{intent_guidance}{qualification_section}{script_section}{step_section}{context_section}{action_section}"""
        return system_instruction.strip()

    @staticmethod
    def clean_response(text: str) -> str:
        """
        Cleans any internal markers, emotion tags, metadata headers, markdown formatting, or artifacts from generated text.
        """
        if not text:
            return ""
        from app.services.ai.response_sanitizer import ResponseSanitizer
        return ResponseSanitizer.clean_customer_facing_text(text, cut_off_script_branches=False)

