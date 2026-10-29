"""
Module: Real-Time Voice Emotion Detection & Dynamic Agent Tone Controller.

Analyzes acoustic prosodic signals (speaking rate, pitch variation, volume/intensity,
pause ratio, vocal energy) combined with text sentiment and context cues.

Provides:
- Configurable emotion taxonomy and thresholds
- Confidence and intensity scoring
- Emotion smoothing / temporal aggregation to avoid rapid tone jitter
- Tone mappings (calm_empathetic, patient_explanatory, warm_positive, direct_concise, professional_natural)
- Tone parameter generation (speaking_rate, pitch, stability, similarity, style, empathy_level, response_length)
"""

from typing import Any, Dict, Optional

EMOTION_TAXONOMY = [
    "neutral",
    "positive",
    "happy",
    "frustrated",
    "angry",
    "confused",
    "concerned",
    "sad",
    "impatient",
    "urgent",
]

# Configurable Confidence Thresholds
CONFIDENCE_THRESHOLDS = {
    "LOW": 0.50,      # Below this: maintain normal professional tone
    "MEDIUM": 0.75,   # At/above this: mild to moderate tone adjustment
    "HIGH": 0.90,     # At/above this: strong tone adjustment & escalation triggers
}

# Dynamic Tone Mappings
TONE_MAP = {
    "frustrated": "calm_empathetic",
    "angry": "calm_empathetic",
    "confused": "patient_explanatory",
    "happy": "warm_positive",
    "positive": "warm_positive",
    "urgent": "direct_concise",
    "impatient": "direct_concise",
    "concerned": "calm_empathetic",
    "sad": "calm_empathetic",
    "neutral": "professional_natural",
}

# TTS Parameter Mappings per Tone
TONE_PARAMETERS = {
    "calm_empathetic": {
        "speaking_rate": 0.90,
        "pitch": "-2%",
        "energy": "moderate",
        "response_length": "short",
        "pause_between_sentences": "short",
        "empathy_level": "high",
        "elevenlabs": {"stability": 0.75, "similarity_boost": 0.85, "style": 0.10},
        "sarvam": {"pitch": -0.15, "pace": 0.90},
        "llm_instruction": (
            "The customer feels frustrated or concerned. Remain very calm, highly empathetic, and non-defensive. "
            "Acknowledge their feeling directly without arguing or excessive explanations, and provide a direct solution."
        ),
    },
    "patient_explanatory": {
        "speaking_rate": 0.85,
        "pitch": "normal",
        "energy": "gentle",
        "response_length": "short_step_by_step",
        "pause_between_sentences": "moderate",
        "empathy_level": "high",
        "elevenlabs": {"stability": 0.65, "similarity_boost": 0.80, "style": 0.05},
        "sarvam": {"pitch": 0.0, "pace": 0.85},
        "llm_instruction": (
            "The customer is confused or uncertain. Slow down, use simple language, break information into small "
            "step-by-step pieces, and ask only ONE clear question at a time."
        ),
    },
    "warm_positive": {
        "speaking_rate": 1.05,
        "pitch": "+3%",
        "energy": "enthusiastic",
        "response_length": "natural",
        "pause_between_sentences": "normal",
        "empathy_level": "moderate",
        "elevenlabs": {"stability": 0.35, "similarity_boost": 0.80, "style": 0.45},
        "sarvam": {"pitch": 0.25, "pace": 1.08},
        "llm_instruction": (
            "The customer is happy and positive. Match their energy with a warm, friendly tone and natural enthusiasm."
        ),
    },
    "direct_concise": {
        "speaking_rate": 1.10,
        "pitch": "normal",
        "energy": "high",
        "response_length": "very_short",
        "pause_between_sentences": "short",
        "empathy_level": "moderate",
        "elevenlabs": {"stability": 0.50, "similarity_boost": 0.75, "style": 0.20},
        "sarvam": {"pitch": 0.10, "pace": 1.10},
        "llm_instruction": (
            "The customer is in a hurry or urgent. Provide a direct, highly concise response with zero fluff. "
            "Prioritize their request immediately."
        ),
    },
    "professional_natural": {
        "speaking_rate": 1.00,
        "pitch": "normal",
        "energy": "normal",
        "response_length": "standard",
        "pause_between_sentences": "normal",
        "empathy_level": "standard",
        "elevenlabs": {"stability": 0.50, "similarity_boost": 0.75, "style": 0.00},
        "sarvam": {"pitch": 0.0, "pace": 1.00},
        "llm_instruction": (
            "Maintain a clear, professional, and natural conversational tone."
        ),
    },
}


def analyze_voice_emotion(
    speech_text: str,
    audio_features: Optional[Dict[str, Any]] = None,
    previous_emotion_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Analyzes customer speech and acoustic signals to estimate emotional state,
    confidence, intensity, and dynamic agent response tone.
    
    Applies emotion smoothing across turns to prevent erratic tone jitter.
    """
    text = (speech_text or "").strip()
    lowered = text.lower()
    audio = audio_features or {}

    # 1. Lexical / Sentiment Keyword Signals
    frustrated_keywords = [
        "frustrated", "angry", "annoyed", "ridiculous", "terrible", "worst",
        "waste of time", "already told", "three times", "multiple times", "no one helped",
        "manager", "supervisor", "sick of this", "hate this", "stupid", "unacceptable",
        "nonsense", "stop calling", "why is this so hard", "not what i asked",
    ]
    confused_keywords = [
        "don't understand", "confused", "what do you mean", "huh", "pardon",
        "lost me", "complicated", "not clear", "which one", "what is this",
        "where do i", "how does this work",
    ]
    positive_keywords = [
        "great", "awesome", "perfect", "thank you", "thanks", "amazing", "wonderful",
        "love it", "sounds good", "excellent", "super", "appreciate it", "happy",
    ]
    urgent_keywords = [
        "asap", "urgent", "hurry", "right now", "emergency", "immediately",
        "running late", "no time", "quick question", "fast",
    ]

    frust_score = sum(1 for kw in frustrated_keywords if kw in lowered)
    conf_score = sum(1 for kw in confused_keywords if kw in lowered)
    pos_score = sum(1 for kw in positive_keywords if kw in lowered)
    urg_score = sum(1 for kw in urgent_keywords if kw in lowered)

    # 2. Acoustic & Prosodic Signals (if available)
    speaking_rate = audio.get("speaking_rate", 1.0)
    pitch_var = audio.get("pitch_variation", 1.0)
    volume = audio.get("volume_intensity", 1.0)
    pause_ratio = audio.get("pause_ratio", 0.1)
    interruption = audio.get("is_interruption", False)

    # Signal fusion logic
    raw_emotion = "neutral"
    raw_confidence = 0.40
    raw_intensity = 0.30

    if frust_score >= 2 or (frust_score >= 1 and (volume > 1.3 or interruption)):
        raw_emotion = "angry" if (frust_score >= 2 and volume > 1.2) else "frustrated"
        raw_confidence = min(0.95, 0.65 + (frust_score * 0.12) + (0.10 if interruption else 0))
        raw_intensity = min(0.95, 0.60 + (frust_score * 0.10) + (volume - 1.0) * 0.2)
    elif conf_score >= 1 or pause_ratio > 0.35:
        raw_emotion = "confused"
        raw_confidence = min(0.90, 0.60 + (conf_score * 0.15) + (pause_ratio * 0.4))
        raw_intensity = min(0.85, 0.50 + (conf_score * 0.10))
    elif pos_score >= 1 and not frust_score:
        raw_emotion = "happy" if pos_score >= 2 else "positive"
        raw_confidence = min(0.92, 0.65 + (pos_score * 0.12))
        raw_intensity = min(0.85, 0.55 + (pos_score * 0.10))
    elif urg_score >= 1 or (speaking_rate > 1.3 and volume > 1.1):
        raw_emotion = "urgent"
        raw_confidence = min(0.90, 0.60 + (urg_score * 0.15))
        raw_intensity = min(0.90, 0.60 + (speaking_rate - 1.0) * 0.5)
    elif frust_score == 1:
        raw_emotion = "frustrated"
        raw_confidence = 0.65
        raw_intensity = 0.55
    else:
        raw_emotion = "neutral"
        raw_confidence = 0.50
        raw_intensity = 0.30

    # Noise immunity guard: loud volume alone without text clues or interruption = normal speech
    if raw_emotion in ("angry", "frustrated") and frust_score == 0 and not interruption:
        raw_emotion = "neutral"
        raw_confidence = 0.45
        raw_intensity = 0.35

    # 3. Temporal Emotion Smoothing / Aggregation across turns
    if previous_emotion_state is None:
        smoothed_confidence = round(raw_confidence, 2)
        smoothed_intensity = round(raw_intensity, 2)
        prev_emo = "neutral"
        prev_consecutive_frust = 0
    else:
        prev_emo = previous_emotion_state.get("emotion", "neutral")
        prev_conf = previous_emotion_state.get("confidence", 0.5)
        prev_int = previous_emotion_state.get("intensity", 0.3)
        prev_consecutive_frust = previous_emotion_state.get("consecutive_frustrated_count", 0)
        smoothed_confidence = round(0.7 * raw_confidence + 0.3 * prev_conf, 2)
        smoothed_intensity = round(0.7 * raw_intensity + 0.3 * prev_int, 2)

    if raw_confidence < CONFIDENCE_THRESHOLDS["LOW"] and prev_emo != "neutral":
        effective_emotion = prev_emo if prev_conf >= CONFIDENCE_THRESHOLDS["MEDIUM"] else "neutral"
    else:
        effective_emotion = raw_emotion

    if effective_emotion in ("frustrated", "angry"):
        consecutive_frustrated_count = prev_consecutive_frust + 1
    else:
        consecutive_frustrated_count = max(0, prev_consecutive_frust - 1)

    escalation_recommended = (
        consecutive_frustrated_count >= 2
        or "human" in lowered
        or ("agent" in lowered and "speak" in lowered)
        or "representative" in lowered
        or "transfer me" in lowered
    )

    # 4. Map Emotion to Tone & Parameters
    if smoothed_confidence < CONFIDENCE_THRESHOLDS["LOW"]:
        selected_tone = "professional_natural"
    else:
        selected_tone = TONE_MAP.get(effective_emotion, "professional_natural")

    tone_params = TONE_PARAMETERS.get(selected_tone, TONE_PARAMETERS["professional_natural"])

    return {
        "emotion": effective_emotion,
        "confidence": smoothed_confidence,
        "intensity": smoothed_intensity,
        "raw_emotion": raw_emotion,
        "tone": selected_tone,
        "speaking_rate": tone_params["speaking_rate"],
        "pitch": tone_params["pitch"],
        "energy": tone_params["energy"],
        "response_length": tone_params["response_length"],
        "empathy_level": tone_params["empathy_level"],
        "elevenlabs": tone_params["elevenlabs"],
        "sarvam": tone_params["sarvam"],
        "llm_instruction": tone_params["llm_instruction"],
        "consecutive_frustrated_count": consecutive_frustrated_count,
        "escalation_recommended": bool(escalation_recommended),
    }
