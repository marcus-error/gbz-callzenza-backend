"""
Transcript Validator Service.
Validates STT speech transcripts before passing them to RAG/LLM.
Handles empty audio, noise tokens, STT hallucinations, garbled speech,
while preserving legitimate short answers like 'Yes', 'No', 'Tomorrow', '10 AM'.
"""
from __future__ import annotations

import re
from typing import Dict, Any, Optional
from loguru import logger


STT_HALLUCINATION_PATTERNS = [
    r'(?i)^(?:thank\s+you(?:\s+for\s+watching)?[\!\.\?]*$)',
    r'(?i)^(?:subtitles\s+by.*$)',
    r'(?i)^(?:amara\.org.*$)',
    r'(?i)^(?:copyright.*$)',
    r'(?i)^(?:translated\s+by.*$)',
    r'(?i)^(?:bye|goodbye|see\s+you\s+(?:next\s+time|later))[\!\.\?]*$',
    r'(?i)^(?:uh+|um+|mhm+|ah+|huh+|a|the|you)[\!\.\?]*$',
    r'(?i)^\[(?:silence|music|noise|cough|blank_audio|snort|coughing)\]$',
]

LEGITIMATE_SHORT_ANSWERS = {
    # English
    "yes", "yeah", "yep", "ya", "sure", "ok", "okay", "fine", "correct", "right", "true", "great", "good",
    "no", "nope", "nah", "never", "not really", "cancel",
    "tomorrow", "today", "tonight", "morning", "afternoon", "evening", "now",
    "10 am", "10am", "11 am", "11am", "12 pm", "12pm", "1 pm", "1pm", "2 pm", "2pm", "3 pm", "3pm", "4 pm", "4pm", "5 pm", "5pm",
    # Tamil
    "ஆமாம்", "சரி", "ஆம்", "இல்லை", "வேண்டாம்", "நாளை", "இன்று", "காலை", "மாலை",
    # Hindi
    "हाँ", "ठीक है", "जी", "नहीं", "ना", "कल", "आज",
}


def get_repeat_prompt(language: str = "en") -> str:
    """Returns a polite prompt asking the caller to repeat in their conversation language."""
    lang = (language or "en").lower()
    if lang in ("ta", "tamil"):
        return "மன்னிக்கவும், நீங்கள் பேசியது தெளிவாக கேட்கவில்லை. மீண்டும் கூற முடியுமா?"
    elif lang in ("hi", "hindi"):
        return "क्षमा करें, मुझे आपकी आवाज़ साफ नहीं सुनाई दी। क्या आप कृपया फिर से दोहरा सकते हैं?"
    elif lang in ("te", "telugu"):
        return "క్షమించండి, మీ స్వరం స్పష్టంగా వినిపించలేదు. దయచేసి మళ్ళీ చెప్పగలరా?"
    elif lang in ("ml", "malayalam"):
        return "ക്ഷമിക്കണം, താങ്കൾ പറഞ്ഞത് വ്യക്തമായില്ല. ദയവായി ഒന്നുകൂടി പറയാമോ?"
    elif lang in ("kn", "kannada"):
        return "<ctrl42>ಕ್ಷಮಿಸಿ, ನಿಮ್ಮ ಧ್ವನಿ ಸ್ಪಷ್ಟವಾಗಿ ಕೇಳಿಸಲಿಲ್ಲ. దయವಿಟ್ಟು ಮತ್ತೊಮ್ಮೆ ಹೇಳುತ್ತೀರಾ?"
    else:
        return "I'm sorry, I didn't quite catch that. Could you please repeat that?"


class TranscriptValidator:
    """Validates STT transcripts before RAG and LLM execution."""

    @classmethod
    def validate_transcript(
        cls,
        transcript: str,
        language: str = "en",
        previous_transcript: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Validates user transcript.
        Returns dict:
          {
            "is_valid": bool,
            "reason": str, # "VALID", "EMPTY", "NOISE", "HALLUCINATION", "DUPLICATE"
            "repeat_prompt": str | None
          }
        """
        if not transcript or not transcript.strip():
            logger.info("[TranscriptValidator] Empty transcript detected.")
            return {
                "is_valid": False,
                "reason": "EMPTY",
                "repeat_prompt": get_repeat_prompt(language),
            }

        clean = transcript.strip()
        clean_lower = clean.lower()

        # 1. Non-alphanumeric noise / punctuation only
        if not any(c.isalnum() for c in clean):
            logger.info(f"[TranscriptValidator] Punctuation/noise-only transcript: '{clean}'")
            return {
                "is_valid": False,
                "reason": "NOISE",
                "repeat_prompt": get_repeat_prompt(language),
            }

        # 2. Check STT Hallucinations
        for pat in STT_HALLUCINATION_PATTERNS:
            if re.search(pat, clean):
                logger.info(f"[TranscriptValidator] STT hallucination pattern matched: '{clean}'")
                return {
                    "is_valid": False,
                    "reason": "HALLUCINATION",
                    "repeat_prompt": get_repeat_prompt(language),
                }

        # 3. Check for repeated single-character or single-token spam (e.g. "a a a a a")
        words = clean_lower.split()
        if len(words) > 3 and len(set(words)) == 1:
            logger.info(f"[TranscriptValidator] Single-token repetitive noise: '{clean}'")
            return {
                "is_valid": False,
                "reason": "NOISE",
                "repeat_prompt": get_repeat_prompt(language),
            }

        # 4. Check for exact duplicate transcript loop from previous turn
        if previous_transcript and clean_lower == previous_transcript.strip().lower() and clean_lower not in LEGITIMATE_SHORT_ANSWERS:
            logger.info(f"[TranscriptValidator] Duplicate consecutive transcript loop: '{clean}'")
            return {
                "is_valid": False,
                "reason": "DUPLICATE",
                "repeat_prompt": get_repeat_prompt(language),
            }

        # 5. Legitimate short answers check (Passes immediately)
        if clean_lower in LEGITIMATE_SHORT_ANSWERS:
            logger.debug(f"[TranscriptValidator] Legitimate short answer validated: '{clean}'")
            return {
                "is_valid": True,
                "reason": "VALID",
                "repeat_prompt": None,
            }

        # 6. Title / Name / Time / Date / Number regex exemptions (e.g. "Dr. Priya", "10:30 AM", "123")
        if re.search(r'^(?:Dr\.|Doctor|Mr\.|Mrs\.|Ms\.)\s+[A-Za-z]+', clean, re.I):
            return {"is_valid": True, "reason": "VALID", "repeat_prompt": None}

        if re.search(r'^\d{1,2}(?::\d{2})?\s*(?:am|pm)?$', clean_lower):
            return {"is_valid": True, "reason": "VALID", "repeat_prompt": None}

        return {
            "is_valid": True,
            "reason": "VALID",
            "repeat_prompt": None,
        }
