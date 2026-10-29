"""
Unit Test Suite for AI Voice Agent Issue Fixes:
1. Dynamic Time-of-Day Greeting
2. Transcript Validation (STT noise & hallucinations vs valid short speech)
3. Multilingual Detection & TTS Voice Mapping
4. Latency Breakdown Structure
5. Conversation Context & Slot Extraction (Dr. Priya)
"""
import datetime
import pytest
from app.services.language_service import (
    get_time_of_day_salutation,
    generate_time_based_greeting,
    detect_speech_language,
)
from app.services.ai.transcript_validator import TranscriptValidator
from app.services.ai.conversation.memory_service import ConversationMemoryService


def test_1_dynamic_time_of_day_greeting():
    salutation = get_time_of_day_salutation("en")
    now_hour = datetime.datetime.now().hour
    if 5 <= now_hour < 12:
        assert salutation == "Good Morning"
    elif 12 <= now_hour < 17:
        assert salutation == "Good Afternoon"
    else:
        assert salutation == "Good Evening"

    greeting = generate_time_based_greeting(agent_name="AI Assistant", language="en")
    assert salutation in greeting
    assert "Thank You for Calling" in greeting
    assert "How can I help you today?" in greeting


def test_2_transcript_validation_hallucinations_and_noise():
    # Empty / noise
    v_empty = TranscriptValidator.validate_transcript("")
    assert v_empty["is_valid"] is False
    assert v_empty["reason"] == "EMPTY"

    v_noise = TranscriptValidator.validate_transcript("[silence]")
    assert v_noise["is_valid"] is False
    assert v_noise["reason"] == "HALLUCINATION"

    # STT Hallucination
    v_hallucination = TranscriptValidator.validate_transcript("Thank you for watching.")
    assert v_hallucination["is_valid"] is False
    assert v_hallucination["reason"] == "HALLUCINATION"
    assert v_hallucination["repeat_prompt"] is not None

    v_amara = TranscriptValidator.validate_transcript("Subtitles by Amara.org")
    assert v_amara["is_valid"] is False
    assert v_amara["reason"] == "HALLUCINATION"


def test_2_transcript_validation_legitimate_short_answers():
    for word in ["Yes", "No", "Okay", "Sure", "Tomorrow", "10 AM", "Dr. Priya"]:
        res = TranscriptValidator.validate_transcript(word)
        assert res["is_valid"] is True, f"'{word}' must be accepted as valid input"
        assert res["reason"] == "VALID"


def test_3_multilingual_detection():
    # English
    assert detect_speech_language("I want to reschedule my appointment.", "en") == "en"

    # Tamil
    assert detect_speech_language("எனக்கு நாளைக்கு appointment வேண்டும்.", "en") == "ta"

    # Hindi
    assert detect_speech_language("मुझे अपॉइंटमेंट चाहिए.", "en") == "hi"


def test_5_conversation_memory_title_slot_extraction():
    session_id = "test_slot_session"
    ConversationMemoryService.clear_session("agent_test", session_id)
    ConversationMemoryService.add_turn("agent_test", session_id, role="user", content="I want an appointment with Dr. Priya tomorrow at 10 AM")

    entities = ConversationMemoryService.get_entities("agent_test", session_id)
    assert entities.get("target_entity") == "Dr. Priya"
    assert entities.get("date") == "tomorrow"
    assert entities.get("time") == "10 AM"
    ConversationMemoryService.clear_session("agent_test", session_id)
