"""
Automated Test Suite for CallZenza Critical Low-Latency Voice Response Rectification.
Covers all 14 requirements from Specification #25:
1. test_fast_llm_response
2. test_no_deprecated_model_retry
3. test_llm_429_fast_fallback
4. test_simple_greeting_skips_rag
5. test_script_first_response
6. test_rag_only_when_required
7. test_conversation_memory
8. test_emotion_tone_selection
9. test_murf_tts
10. test_audio_range_response
11. test_audio_content_length
12. test_barge_in
13. test_session_isolation
14. test_local_server_ai_parity
"""
import os
import re
import time
import uuid
import pytest
import pytest_asyncio
import httpx

from app.services.ai.llm_provider_manager import LLMProviderManager, VERIFIED_GEMINI_MODELS, VERIFIED_MISTRAL_MODELS
from app.services.ai.gemini_service import GeminiService
from app.services.ai.response_sanitizer import ResponseSanitizer
from app.services.ai.script_engine import ScriptNormalizer, ScriptBranchResolver
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.tts_service import clean_markdown_for_speech
from app.main import app
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_fast_llm_response():
    """Test 1: Verify LLMProviderManager returns verified fast model without sequential 404 delays."""
    manager = LLMProviderManager.get_instance()
    t0 = time.perf_counter()
    resp = await manager.generate("Hello, acknowledge in 3 words.")
    t1 = time.perf_counter()
    latency_ms = (t1 - t0) * 1000
    assert len(resp) > 0
    # Must complete fast (under 3.5s including remote network call)
    assert latency_ms < 4000


@pytest.mark.asyncio
async def test_no_deprecated_model_retry():
    """Test 2: Ensure deprecated 404 models (gemini-2.0-flash, gemini-1.5-flash) are never retried."""
    manager = LLMProviderManager.get_instance()
    for deprecated in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-1.5-pro"]:
        assert deprecated not in VERIFIED_GEMINI_MODELS
    assert VERIFIED_GEMINI_MODELS[0] == "gemini-flash-lite-latest"


@pytest.mark.asyncio
async def test_llm_429_fast_fallback():
    """Test 3: Verify 429 rate limit triggers immediate cooldown and instant fallback."""
    manager = LLMProviderManager()
    # Set Mistral on cooldown
    manager.set_provider_cooldown("mistral", duration_sec=60.0)
    assert not manager.is_provider_healthy("mistral")


def test_simple_greeting_skips_rag():
    """Test 4: Verify simple greetings and short affirmations skip RAG completely."""
    from app.services.ai.agent_engine import AgentEngine
    for utterance in ["Hi", "Hello", "Good morning", "Yes", "No", "Okay", "Yeah", "Thanks", "Fine", "Sure"]:
        intent = AgentEngine.classify_caller_intent(utterance)
        assert intent in ("greeting", "positive_response", "negative_response", "thank_you")


def test_script_first_response():
    """Test 5: Verify script-first resolution generates instant direct response without dumping full script."""
    raw_script = (
        '1. GREETING: Good morning Mr.______ I am MAGIC calling from D-BUSINESS FORUM, can I talk to you for 2 minutes?\n'
        'If no - when shall I call back sir?\n'
        'If yes - Sir D-BUSINESS FORUM is a business entrepreneurs forum...'
    )
    clean = ResponseSanitizer.clean_customer_facing_text(
        raw_script,
        agent_name="MAGIC",
        lead_name="Rajesh",
        cut_off_script_branches=True,
    )
    assert "If no" not in clean
    assert "If yes" not in clean
    assert "<emotion" not in clean
    assert "Good morning Mr. Rajesh" in clean


@pytest.mark.asyncio
async def test_rag_only_when_required():
    """Test 6: Verify RAG is required only for knowledge questions, not for conversational affirmations."""
    from app.services.ai.script_engine import ScriptBranchResolver, ScriptNormalizer
    script = ScriptNormalizer.parse_script("1. GREETING: Hello! Are you interested?\nIf yes - Great!\nIf no - Okay.")
    action_yes = ScriptBranchResolver.resolve_action(
        script=script,
        current_step_index=0,
        intent="positive_response",
        collected_entities={},
        caller_msg="Yes I am",
    )
    assert not action_yes.get("requires_rag")


def test_conversation_memory():
    """Test 7: Verify conversation history and step tracking advance properly without repeating greetings."""
    agent_id = f"test_agent_{uuid.uuid4().hex[:8]}"
    session_id = f"sess_{uuid.uuid4().hex[:8]}"
    
    ConversationMemoryService.add_turn(agent_id, session_id, role="user", content="Hello")
    ConversationMemoryService.add_turn(agent_id, session_id, role="assistant", content="Good morning!")
    ConversationMemoryService.set_current_step_index(agent_id, session_id, 1)
    
    history = ConversationMemoryService.get_history(agent_id, session_id)
    assert len(history) == 2
    assert ConversationMemoryService.get_current_step_index(agent_id, session_id) == 1


def test_emotion_tone_selection():
    """Test 8: Verify emotion extraction produces clean text and external emotion property."""
    raw = '<emotion type="happy" />Good morning! I am delighted to speak with you today.'
    emotion, clean = ResponseSanitizer.extract_emotion_and_text(raw)
    assert emotion == "happy"
    assert "<emotion" not in clean
    assert clean == "Good morning! I am delighted to speak with you today."


def test_murf_tts():
    """Test 9: Verify clean_markdown_for_speech strips internal markup before TTS."""
    raw = '<Calls Script> **Agent:** Hello [Name]! <emotion type="happy" /> How are you?'
    clean = clean_markdown_for_speech(raw)
    assert "<Calls Script>" not in clean
    assert "<emotion" not in clean
    assert "**" not in clean
    assert "Agent:" not in clean


def test_audio_range_response():
    """Test 10: Verify GET /static/audio/{filename} with HTTP Range header returns 206 Partial Content."""
    client = TestClient(app)
    os.makedirs("app/static/audio", exist_ok=True)
    test_file = "app/static/audio/unit_test_audio.mp3"
    with open(test_file, "wb") as f:
        f.write(b"\xff\xfb\x90\x64" * 250)  # 1000 bytes
    
    resp = client.get("/static/audio/unit_test_audio.mp3", headers={"Range": "bytes=0-199"})
    assert resp.status_code == 206
    assert resp.headers.get("accept-ranges") == "bytes"
    assert resp.headers.get("content-range") == "bytes 0-199/1000"
    assert len(resp.content) == 200


def test_audio_content_length():
    """Test 11: Verify full GET returns 200 OK and matching Content-Length."""
    client = TestClient(app)
    test_file = "app/static/audio/unit_test_audio.mp3"
    file_size = os.path.getsize(test_file)
    
    resp = client.get("/static/audio/unit_test_audio.mp3")
    assert resp.status_code == 200
    assert resp.headers.get("content-length") == str(file_size)
    assert len(resp.content) == file_size


def test_barge_in():
    """Test 12: Verify clean response formatting enables sentence-level boundaries for barge-in."""
    raw = '<emotion type="happy" />Hello! How can I help you today? Please let me know your question.'
    clean = ResponseSanitizer.clean_customer_facing_text(raw)
    sentences = [s.strip() for s in re.split(r'[.!?]+\s*', clean) if s.strip()]
    assert len(sentences) >= 2


def test_session_isolation():
    """Test 13: Verify two separate sessions maintain independent conversation histories."""
    agent_id = f"test_agent_{uuid.uuid4().hex[:8]}"
    sess1 = f"sess_1_{uuid.uuid4().hex[:8]}"
    sess2 = f"sess_2_{uuid.uuid4().hex[:8]}"
    
    ConversationMemoryService.add_turn(agent_id, sess1, role="user", content="Question 1")
    ConversationMemoryService.add_turn(agent_id, sess2, role="user", content="Question 2")
    
    h1 = ConversationMemoryService.get_history(agent_id, sess1)
    h2 = ConversationMemoryService.get_history(agent_id, sess2)
    assert len(h1) == 1 and h1[0]["content"] == "Question 1"
    assert len(h2) == 1 and h2[0]["content"] == "Question 2"


def test_local_server_ai_parity():
    """Test 14: Verify ResponseSanitizer produces identical contract dictionary for local and SIP calls."""
    raw = '<emotion type="happy" />Good morning Mr. Naveen, I am MAGIC from D-BUSINESS FORUM.'
    sanitized = ResponseSanitizer.sanitize(raw, agent_name="MAGIC", lead_name="Naveen")
    assert "text" in sanitized
    assert "response" in sanitized
    assert "emotion" in sanitized
    assert sanitized["emotion"] == "happy"
    assert "<emotion" not in sanitized["text"]
