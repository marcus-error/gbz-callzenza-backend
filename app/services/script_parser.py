"""
Script Parser Service for CallZenza Campaign Scripts.

Parses structured <emotion type="..." /> and <break time="...s" /> markup tags
into machine-readable speech and pause segments for TTS generation and voice playback.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel, Field


class EmotionType(str, Enum):
    HAPPY = "happy"
    SAD = "sad"
    FEARFUL = "fearful"
    DISGUSTED = "disgusted"
    SURPRISED = "surprised"
    NEUTRAL = "neutral"
    FLUENT = "fluent"


VALID_EMOTIONS = {e.value for e in EmotionType}

MIN_PAUSE_SECONDS = 0.1
MAX_PAUSE_SECONDS = 10.0

# Regex pattern matching <emotion type="..." /> and <break time="...s" />
EMOTION_REGEX = re.compile(
    r'<emotion\s+type=["\']?([a-zA-Z]+)["\']?\s*/?>',
    re.IGNORECASE
)
BREAK_REGEX = re.compile(
    r'<break\s+time=["\']?(\d*\.?\d+)s?["\']?\s*/?>',
    re.IGNORECASE
)
COMBINED_TAG_REGEX = re.compile(
    r'(<emotion\s+type=["\']?([a-zA-Z]+)["\']?\s*/?>)|'
    r'(<break\s+time=["\']?(\d*\.?\d+)s?["\']?\s*/?>)',
    re.IGNORECASE
)


class SpeechSegment(BaseModel):
    type: str = Field(..., description="'speech' or 'pause'")
    emotion: Optional[str] = Field(None, description="Active emotion for speech segments (e.g. happy, sad)")
    text: Optional[str] = Field(None, description="Spoken dialogue text")
    duration: Optional[float] = Field(None, description="Pause duration in seconds")


class ScriptParseResult(BaseModel):
    segments: List[SpeechSegment]
    total_speech_segments: int
    total_pause_duration: float
    detected_emotions: List[str]


def validate_script_markup(script: str) -> Tuple[bool, Optional[str]]:
    """Validates emotion and pause tags in script string."""
    if not script:
        return True, None

    # Validate all emotion tags
    for match in EMOTION_REGEX.finditer(script):
        raw_emo = match.group(1).lower()
        if raw_emo not in VALID_EMOTIONS:
            sorted_allowed = ", ".join(sorted(list(VALID_EMOTIONS)))
            return False, f"Unsupported emotion: '{match.group(1)}'. Allowed emotions are: {sorted_allowed}."

    # Validate all break tags
    for match in BREAK_REGEX.finditer(script):
        raw_dur = match.group(1)
        try:
            dur = float(raw_dur)
            if dur < MIN_PAUSE_SECONDS or dur > MAX_PAUSE_SECONDS:
                return False, f"Invalid pause duration: {dur}s. Allowed range is {MIN_PAUSE_SECONDS}s to {MAX_PAUSE_SECONDS}s."
        except ValueError:
            return False, f"Invalid pause duration format: '{raw_dur}'."

    return True, None


def strip_markup_tags(script: str) -> str:
    """Removes all <emotion ... /> and <break ... /> tags from text."""
    if not script:
        return ""
    cleaned = COMBINED_TAG_REGEX.sub(" ", script)
    # Collapse multiple whitespaces
    return re.sub(r"\s+", " ", cleaned).strip()


def parse_script_segments(
    script: str,
    strict: bool = True,
    lead_name: str = "",
    agent_name: str = "",
) -> List[Dict[str, Any]]:
    """
    Parses a campaign script containing <emotion type="..." /> and <break time="...s" />
    into a sequence of structured speech and pause segments.

    - Active emotion defaults to 'neutral' and applies to following text until another
      emotion tag is encountered.
    - Pauses pause for the exact duration, and speech resumes with the active emotion.
    - Variable placeholders like [Lead Name], [Agent Name], [Name] are substituted if provided.
    """
    if not script or not script.strip():
        return []

    # Optional variable replacement
    text_content = script
    if lead_name:
        text_content = text_content.replace("[Lead Name]", lead_name).replace("[Name]", lead_name)
    if agent_name:
        text_content = text_content.replace("[Agent Name]", agent_name)

    # Validate first if strict mode
    if strict:
        is_valid, err = validate_script_markup(text_content)
        if not is_valid:
            raise ValueError(err)

    segments: List[Dict[str, Any]] = []
    current_emotion = EmotionType.NEUTRAL.value
    last_idx = 0

    for match in COMBINED_TAG_REGEX.finditer(text_content):
        # Text before this tag
        text_chunk = text_content[last_idx:match.start()].strip()
        if text_chunk:
            segments.append({
                "type": "speech",
                "emotion": current_emotion,
                "text": text_chunk,
            })

        emotion_tag = match.group(2)
        break_tag = match.group(4)

        if emotion_tag:
            emo_clean = emotion_tag.lower()
            if emo_clean in VALID_EMOTIONS:
                current_emotion = emo_clean
            else:
                if strict:
                    raise ValueError(f"Unsupported emotion: '{emotion_tag}'")
                current_emotion = EmotionType.NEUTRAL.value

        elif break_tag:
            try:
                duration_val = float(break_tag)
                if strict and (duration_val < MIN_PAUSE_SECONDS or duration_val > MAX_PAUSE_SECONDS):
                    raise ValueError(
                        f"Invalid pause duration: {duration_val}s. Allowed range is {MIN_PAUSE_SECONDS}s to {MAX_PAUSE_SECONDS}s."
                    )
                duration_val = max(MIN_PAUSE_SECONDS, min(MAX_PAUSE_SECONDS, round(duration_val, 2)))
                segments.append({
                    "type": "pause",
                    "duration": duration_val,
                })
            except ValueError as ve:
                if strict:
                    raise ve

        last_idx = match.end()

    # Any remaining text after the last tag
    remaining_text = text_content[last_idx:].strip()
    if remaining_text:
        segments.append({
            "type": "speech",
            "emotion": current_emotion,
            "text": remaining_text,
        })

    return segments
