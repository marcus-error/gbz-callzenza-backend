"""
Universal AI Response Sanitizer & Customer Speech Contract Service.
Ensures the AI output returned to the frontend, TTS (Murf/ElevenLabs/Sarvam),
and SIP calls contains strictly clean, natural customer-facing spoken dialogue.

Never exposes:
- <emotion ...> XML/HTML tags
- <Calls Script> internal labels or markup
- System prompts, instructions, internal reasoning, or tool control objects
- JSON markdown blocks
- Unsectioned campaign script dumps or branching conditions (If yes/If no)
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple


from loguru import logger


class ResponseGuard:
    """
    Response Guard for CallZenza AI Agent responses.
    Enforces that raw RAG context, raw campaign scripts, internal document headers,
    retrieval metadata, or excessive multi-paragraph dumps NEVER reach TTS or the chat UI.
    """

    RAW_PATTERNS = [
        r'(?i)outbound\s+rag\s+script',
        r'(?i)outbound\s+rag',
        r'(?i)rag\s+script',
        r'(?i)rag\s+document',
        r'(?i)script\s+page\s*\d*',
        r'(?i)page\s+\d+\b',
        r'(?i)outbound\s+call\s+opening',
        r'(?i)outbound\s+qualification\s+flow',
        r'(?i)outbound\s+property\s+introduction',
        r'(?i)outbound\s+investment\s+enquiry',
        r'(?i)outbound\s+price\s+handling',
        r'(?i)dream\s+catchers?\s+properties\s+information',
        r'(?i)properties\s+information',
        r'(?i)knowledge\s+base\s+context',
        r'(?i)retrieved\s+knowledge',
        r'(?i)retrieved\s+context',
        r'(?i)document\s+title',
        r'(?i)chunk\s*id\s*:',
        r'(?i)source\s*data\s*:',
        r'(?i)source\s*ids?\b',
        r'(?i)embeddings?\b',
        r'(?i)campaign\s*script\s*&\s*guidelines',
        r'(?i)qualification\s+schema',
        r'(?i)system\s+prompt',
        r'(?i)internal\s+instructions?',
        r'(?i)internal\s+notes?',
        r'(?i)retrieval\s+metadata',
    ]

    SCRIPT_BRANCHING_PATTERNS = [
        r'(?i)\bIf\s+(?:YES|NO|the\s+customer\s+asks|customer\s+asks|user\s+says)\b',
        r'(?i)\bAgent\s+must\s+ask\b',
        r'(?i)\bCustomer\s*:\s*[^\n]+',
        r'(?i)\bAgent\s*:\s*[^\n]+',
    ]

    @classmethod
    def validate_customer_response(cls, text: str) -> Dict[str, Any]:
        """
        Validates if a candidate AI response is safe and customer-facing.
        Returns {"is_valid": True/False, "reason": str}.
        """
        if not text or not text.strip():
            return {"is_valid": False, "reason": "empty_text"}

        cleaned = text.strip()

        # Check raw RAG / script document markers
        for pat in cls.RAW_PATTERNS:
            if re.search(pat, cleaned):
                logger.warning(f"[RESPONSE_GUARD] rejected_raw_context=true | Pattern match: '{pat}'")
                return {"is_valid": False, "reason": f"raw_document_marker:{pat}"}

        # Check script branching instructions
        for pat in cls.SCRIPT_BRANCHING_PATTERNS:
            if re.search(pat, cleaned):
                logger.warning(f"[RESPONSE_GUARD] rejected_raw_context=true | Script branching match: '{pat}'")
                return {"is_valid": False, "reason": f"script_branching_instruction:{pat}"}

        # Check length & document structure: if > 350 chars with 3+ lines/paragraphs, it's a document dump
        lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
        if len(cleaned) > 350 and len(lines) >= 3:
            logger.warning(f"[RESPONSE_GUARD] rejected_raw_context=true | Excessive length document dump ({len(cleaned)} chars, {len(lines)} lines)")
            return {"is_valid": False, "reason": "excessive_length_document_dump"}

        return {"is_valid": True, "reason": "ok"}


class ResponseSanitizer:
    """Central response sanitizer enforcing the customer-facing speech contract."""

    @staticmethod
    def extract_emotion_and_text(raw_text: str) -> Tuple[str, str]:
        """
        Extracts emotion metadata from any XML, tag, or bracketed notation
        and returns (emotion, cleaned_text).
        Emotion remains internal metadata and is never spoken or displayed in bubbles.
        """
        if not raw_text:
            return "neutral", ""

        text = raw_text.strip()
        detected_emotion = "neutral"

        # Match <emotion type="happy" />, <emotion type="happy">...</emotion>, <emotion value="calm">, etc.
        emo_match = re.search(r'<emotion\s+(?:type|value|name)=["\']?([a-zA-Z_\-]+)["\']?\s*(?:/>|>(.*?)</emotion>)', text, re.IGNORECASE | re.DOTALL)
        if emo_match:
            detected_emotion = emo_match.group(1).lower()
            inner_text = emo_match.group(2) if emo_match.group(2) is not None else ""
            # Replace tag with its inner text or empty
            text = text[:emo_match.start()] + inner_text + text[emo_match.end():]
        else:
            # Check for self-closing or bracketed emotion tags: [emotion: happy], <emotion: happy>
            bracket_match = re.search(r'(?:\[|<)emotion:?\s*([a-zA-Z_\-]+)(?:\]|>)', text, re.IGNORECASE)
            if bracket_match:
                detected_emotion = bracket_match.group(1).lower()
                text = text[:bracket_match.start()] + text[bracket_match.end():]

        # Strip any remaining stray <emotion...> or </emotion> tags
        text = re.sub(r'</?emotion[^>]*>', ' ', text, flags=re.IGNORECASE)
        return detected_emotion, text.strip()

    @classmethod
    def clean_customer_facing_text(
        cls,
        raw_text: str,
        agent_name: str = "Alex",
        lead_name: str = "",
        cut_off_script_branches: bool = True,
    ) -> str:
        """
        Purges internal script markup, XML/HTML tags, system headers, markdown,
        and cuts off future campaign branching logic from customer-facing dialogue.
        """
        if not raw_text:
            return ""

        # 1. Extract emotion first
        _, text = cls.extract_emotion_and_text(raw_text)

        # 2. Strip code blocks & JSON wrappers (```json ... ```)
        text = re.sub(r'```(?:json|xml|html)?[\s\S]*?```', ' ', text)
        text = re.sub(r'`[^`]*`', ' ', text)

        # 3. Strip XML / HTML control tags (<Calls Script>, <system>, <tool>, <instruction>, etc.)
        text = re.sub(r'</?(?:Calls?\s*Script|script|system|assistant|tool|internal|instruction|response|context|rag)[^>]*>', ' ', text, flags=re.IGNORECASE)
        # Strip all generic HTML/XML tags
        text = re.sub(r'<[^>]+>', ' ', text)

        # 4. Fill or clean placeholders before markdown stripping
        text = text.replace('[Agent Name]', agent_name).replace('{agent_name}', agent_name)
        text = re.sub(r'\bI am\s+[_\-]{2,}\s+calling\b', f'I am {agent_name} calling', text, flags=re.IGNORECASE)
        text = re.sub(r'\bI am\s+[_\-]{2,}\b', f'I am {agent_name}', text, flags=re.IGNORECASE)

        if lead_name:
            for ph in ['[Name]', '[Customer Name]', '[Lead Name]', '{customer_name}', '{lead_name}', '{name}']:
                text = text.replace(ph, lead_name)
            text = re.sub(r'\b(Mr\.|Mrs\.|Ms\.|Dr\.)\s*_{2,}', rf'\1 {lead_name}', text)
            text = re.sub(r'_{2,}', lead_name, text)
        else:
            text = re.sub(r'\b(?:Mr\.|Mrs\.|Ms\.|Dr\.)\s*(?:_{2,}|\[(?:Name|Customer Name|Lead Name)\])', '', text)
            text = re.sub(r'\[(?:Name|Customer Name|Lead Name)\]', '', text)
        # Resolve time-of-day slash greetings (e.g. "Good morning/afternoon/evening" -> "Good morning")
        try:
            from app.services.language_service import get_time_of_day_salutation
            salutation = get_time_of_day_salutation("en")
        except Exception:
            from datetime import datetime
            hr = datetime.now().hour
            salutation = "Good morning" if 5 <= hr < 12 else ("Good afternoon" if 12 <= hr < 17 else "Good evening")
        text = re.sub(r'Good\s+(?:morning|afternoon|evening)\s*(?:/\s*(?:morning|afternoon|evening))+', salutation, text, flags=re.IGNORECASE)

        # 5. Strip markdown bold / italic formatting (e.g. **Agent:** or *emphasis*)
        text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)
        text = re.sub(r'\*(.*?)\*', r'\1', text)
        text = re.sub(r'__(.*?)__', r'\1', text)
        text = re.sub(r'(?<!\w)_(.*?)(?!\w)_', r'\1', text)
        text = re.sub(r'[`#~]', '', text)

        # 6. Remove bracketed document/chunk metadata tags: [Doc-1], [Doc-Chunk 1], [Chunk 2], etc.
        text = re.sub(r'\[\s*(?:Doc|Chunk|Doc-Chunk|Document|Page)[^\]]*\]:?', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'\[[^\]]*\b(?:Doc|Chunk|Page)\b[^\]]*\]:?', ' ', text, flags=re.IGNORECASE)
        text = re.sub(r'(?i)chunk\s*id\s*:\s*\S+', ' ', text)
        text = re.sub(r'(?i)\bchunk\s*\d+\b:?', ' ', text)

        # 7. Remove internal script labels and role headers at the start or inline
        text = re.sub(r'(?i)^\s*(?:Calls?\s*Script[:\s\-]*|Script[:\s\-]*|Campaign\s*Script[:\s\-]*|Agent\s*Script[:\s\-]*)', ' ', text)
        text = re.sub(r'(?i)\b(?:1\.\s*GREETING[^\n:]*[:\n\-]*|Greeting[:\n\-]*|Stage\s*1[^\n:]*[:\n\-]*)', ' ', text)
        text = re.sub(r'(?i)^\s*(?:answer|response|agent|bot|assistant|rep|alex)\s*:\s*', ' ', text)

        # 8. Remove "According to [context/document]:" and RAG metadata prefixes
        text = re.sub(
            r'(?i)^\s*(?:according to (?:the )?(?:knowledge base|retrieved context|provided document|context|documents)[^:\n]*:?\s*)',
            ' ', text
        )
        text = re.sub(
            r'(?i)^\s*(?:based on (?:the )?(?:knowledge base|retrieved context|provided document|context|documents)[^:\n]*:?\s*)',
            ' ', text
        )
        text = re.sub(r'(?i)^\s*(?:dream\s+catchers?\s+properties\s+\|\s+outbound\s+rag\s+script\s*(?:page\s*\d+)?\s*)', ' ', text)

        # 8b. Strip system prompt leaks and internal directive headers
        internal_leak_patterns = [
            r'(?i)^\s*[-*]?\s*(?:you must speak solely in|you must speak in|speak solely in|speak in|speak naturally in|you must speak naturally in)\s+[a-z\-]+[\.\n:]*',
            r'(?i)^\s*[-*]?\s*speak naturally[,\s]+warmly[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*respond (?:solely )?in (?:the )?customer[\'s]* selected language[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*respond in [a-z\-]+[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*switch to (?:the )?customer[\'s]* requested language[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*strictly follow the (?:agent )?script[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*you are (?:an?|the|a fast[,\s]+natural) ai (?:voice )?agent[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*call direction[:\s]+[a-z\-]+[\.\n:]*',
            r'(?i)^\s*[-*]?\s*strict rule[:\s]+[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*for outbound calls[:\s]+[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*output only the spoken words[^\n.]*[\.\n:]*',
            r'(?i)^\s*[-*]?\s*information already collected[^\n:]*:?',
            r'(?i)^\s*[-*]?\s*specific next action decided[^\n:]*:?',
            r'(?i)^\s*[-*]?\s*current qualification stage[^\n:]*:?',
            r'(?i)^\s*[-*]?\s*assigned agent knowledge[^\n:]*:?',
        ]
        for pat in internal_leak_patterns:
            text = re.sub(pat, '', text).strip()

        # 9. Cut off future conditional branches if raw unsectioned campaign script was passed
        if cut_off_script_branches:
            cutoff_patterns = [
                r'(?:\n|\s+)(?:If\s+(?:no|yes|not|interested|busy|customer|they|the\s+user)\s*[\-:\u2192]|\(If\s+(?:no|yes))',
                r'(?:\n|\s+)(?:Here\s+are\s+some\s+common\s+objections|Common\s+Objections|Objection\s+Handling|\bObjections\b:)',
                r'(?:\n|\s+)(?:2\.\s*|Stage\s*2|Step\s*2|Discovery\s*&|Solution\s*&)',
                r'(?:\n|\s+)(?:OUTBOUND\s+QUALIFICATION\s+FLOW|OUTBOUND\s+PROPERTY\s+INTRODUCTION|OUTBOUND\s+INVESTMENT|OUTBOUND\s+PRICE)',
            ]
            for pattern in cutoff_patterns:
                m = re.search(pattern, text, re.IGNORECASE)
                if m:
                    text = text[:m.start()].strip()

        # 10. Clean markdown links and bullet symbols
        text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
        text = re.sub(r'^[*-]\s+', '', text, flags=re.MULTILINE)

        # 11. Clean dangling commas/whitespace
        text = re.sub(r'\s*,\s*,+', ', ', text)
        text = re.sub(r'^\s*,\s*', '', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    @classmethod
    def sanitize(
        cls,
        raw_text: str,
        agent_name: str = "Alex",
        lead_name: str = "",
        current_step: str = "1. Greeting",
        handoff: bool = False,
        end_call: bool = False,
    ) -> Dict[str, Any]:
        """
        Full contract builder returning structured response dict.
        Guarantees 'text' / 'response_text' has 0 internal control tags.
        """
        emotion, _ = cls.extract_emotion_and_text(raw_text)
        clean_text = cls.clean_customer_facing_text(
            raw_text=raw_text,
            agent_name=agent_name,
            lead_name=lead_name,
            cut_off_script_branches=True,
        )

        return {
            "text": clean_text,
            "response": clean_text,
            "response_text": clean_text,
            "emotion": emotion,
            "should_speak": True,
            "handoff": handoff,
            "end_call": end_call,
            "current_step": current_step,
        }
