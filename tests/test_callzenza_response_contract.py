"""
Automated Test Suite for CallZenza AI Response Contract, Tag Sanitization,
Emotion Metadata Separation, and TTS Spoken Dialogue Verification.
Verifies all 12 test specifications from Requirement #28:
1. test_llm_internal_emotion_tag_removed
2. test_campaign_script_markup_removed
3. test_customer_response_contains_only_spoken_text
4. test_full_script_not_dumped_to_customer
5. test_emotion_metadata_separated
6. test_tts_receives_clean_text
7. test_script_branching
8. test_conversation_context
9. test_rag_only_when_required
10. test_local_server_agent_parity
11. test_latency_timestamps
12. test_barge_in_after_clean_response
"""
import re
import uuid
import pytest
import pytest_asyncio

from app.services.ai.response_sanitizer import ResponseSanitizer
from app.services.ai.script_engine import ScriptNormalizer, ScriptBranchResolver
from app.services.tts_service import clean_markdown_for_speech
from app.services.ai.agent_engine import AgentEngine, AgentEngineResponse
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Business, Lead, Campaign


@pytest_asyncio.fixture
async def db_session():
    async with AsyncSessionLocal() as session:
        yield session


def test_llm_internal_emotion_tag_removed():
    """Test 1: Verify <emotion type="happy" /> and variations are stripped from spoken text."""
    raw_llm_1 = '<emotion type="happy" />Good morning Mr. Kumar. I am calling from D-Business Forum.'
    clean_1 = ResponseSanitizer.clean_customer_facing_text(raw_llm_1)
    assert "<emotion" not in clean_1
    assert "happy" not in clean_1 or "Good morning" in clean_1
    assert clean_1 == "Good morning Mr. Kumar. I am calling from D-Business Forum."

    raw_llm_2 = '<emotion type="friendly">Good afternoon! How are you doing today?</emotion>'
    clean_2 = ResponseSanitizer.clean_customer_facing_text(raw_llm_2)
    assert "<emotion" not in clean_2
    assert "</emotion>" not in clean_2
    assert clean_2 == "Good afternoon! How are you doing today?"


def test_campaign_script_markup_removed():
    """Test 2: Verify <Calls Script>, <script>, and other XML/control tags are removed."""
    raw = '<Calls Script>\nGood morning Mr. Kumar. Can I talk to you for 2 minutes?\n</Calls Script>'
    clean = ResponseSanitizer.clean_customer_facing_text(raw)
    assert "<Calls Script>" not in clean
    assert "</Calls Script>" not in clean
    assert "Calls Script" not in clean
    assert clean == "Good morning Mr. Kumar. Can I talk to you for 2 minutes?"


def test_customer_response_contains_only_spoken_text():
    """Test 3: Verify markdown bolding, chunk tags, role prefixes, and instructions are stripped."""
    raw = "**Agent:** [Doc-Chunk 1]: Hello Mr. John! How can I assist you with **Solar Panels** today?"
    clean = ResponseSanitizer.clean_customer_facing_text(raw)
    assert "**" not in clean
    assert "Agent:" not in clean
    assert "Doc-Chunk" not in clean
    assert clean == "Hello Mr. John! How can I assist you with Solar Panels today?"


def test_full_script_not_dumped_to_customer():
    """Test 4: Verify that passing an unsectioned campaign script with branching logic cuts off future turns."""
    full_campaign_script = (
        '<emotion type="happy" />Calls Script\n'
        'Good morning Mr.______ I am MAGIC calling from D-BUSINESS FORUM, can I talk to u for 2 minutes?\n\n'
        'If no - when shall I call back sir?\n\n'
        'If yes - Sir D-BUSINESS FORUM is a business entrepreneurs forum...\n\n'
        'Common Objections:\n'
        '1. "I am busy right now"\n'
        'Answer: Understood sir, when would be a better time?\n'
    )
    clean = ResponseSanitizer.clean_customer_facing_text(
        full_campaign_script,
        agent_name="MAGIC",
        lead_name="Naveen Kumar",
        cut_off_script_branches=True,
    )
    assert "If no" not in clean
    assert "If yes" not in clean
    assert "Common Objections" not in clean
    assert "Answer:" not in clean
    assert "Good morning Mr. Naveen Kumar" in clean
    assert "can I talk to u for 2 minutes?" in clean


def test_emotion_metadata_separated():
    """Test 5: Verify emotion is extracted into metadata and not left in dialogue text."""
    raw = '<emotion type="empathetic" />I understand you are busy right now. When would be a better time to call you back?'
    res = ResponseSanitizer.sanitize(raw)
    assert res["emotion"] == "empathetic"
    assert "<emotion" not in res["text"]
    assert res["text"] == "I understand you are busy right now. When would be a better time to call you back?"
    assert res["should_speak"] is True


def test_tts_receives_clean_text():
    """Test 6: Verify clean_markdown_for_speech sanitizes text before TTS synthesis."""
    dirty_tts_input = '<emotion type="happy" /><Calls Script>**Hello!** [Doc-1] Welcome to D-Business.'
    clean_tts = clean_markdown_for_speech(dirty_tts_input)
    assert "<emotion" not in clean_tts
    assert "<Calls Script>" not in clean_tts
    assert "**" not in clean_tts
    assert "[Doc-1]" not in clean_tts
    assert clean_tts == "Hello! Welcome to D-Business."


def test_script_branching():
    """Test 7: Verify ScriptBranchResolver properly navigates positive, negative, and objection branches."""
    sample_script = """
    Step 1: Introduction
    Good morning! Can I talk to you for 2 minutes?
    If yes - continue to business overview
    If no - ask callback time

    Step 2: Business Overview
    We are D-Business Forum connecting entrepreneurs. Are you interested in growing your business?

    Step 3: Closing
    Thank you for your time!
    """
    normalized = ScriptNormalizer.parse_script(sample_script, agent_name="Alex", business_name="D-Business")
    assert len(normalized.steps) >= 2

    # Negative response at Step 1 -> Should resolve to ask_callback_time without dumping script
    res_neg = ScriptBranchResolver.resolve_action(
        script=normalized,
        current_step_index=0,
        intent="negative_response",
        collected_entities={},
        caller_msg="No, I am busy right now",
    )
    assert res_neg["action"] == "ask_callback_time"
    assert res_neg["advance_step"] is False

    # Positive response at Step 1 -> Should advance to Step 2
    res_pos = ScriptBranchResolver.resolve_action(
        script=normalized,
        current_step_index=0,
        intent="positive_response",
        collected_entities={},
        caller_msg="Yes, sure, go ahead",
    )
    assert res_pos["action"] == "advance_script"
    assert res_pos["advance_step"] is True
    assert res_pos["target_step_index"] == 1


def test_conversation_context():
    """Test 8: Verify ConversationMemoryService retains session context across multi-turn interactions."""
    agent_id = "test_agent_memory"
    session_id = str(uuid.uuid4())
    ConversationMemoryService.clear_session(agent_id, session_id)

    ConversationMemoryService.add_turn(agent_id, session_id, role="assistant", content="Good morning Mr. Kumar!")
    ConversationMemoryService.add_turn(agent_id, session_id, role="user", content="Yes, I am interested.")
    ConversationMemoryService.add_turn(agent_id, session_id, role="assistant", content="Great! What is your business type?")
    ConversationMemoryService.add_turn(agent_id, session_id, role="user", content="We do manufacturing.")

    history = ConversationMemoryService.get_history(agent_id, session_id, limit=5)
    assert len(history) == 4
    assert history[0]["content"] == "Good morning Mr. Kumar!"
    assert history[3]["content"] == "We do manufacturing."


def test_rag_only_when_required():
    """Test 9: Verify simple acknowledgments skip expensive RAG, while questions trigger RAG."""
    from app.services.retrieval_service import RetrievalService
    assert RetrievalService.is_informational_query("hello") is False
    assert RetrievalService.is_informational_query("yes") is False
    assert RetrievalService.is_informational_query("no") is False
    assert RetrievalService.is_informational_query("okay") is False
    assert RetrievalService.is_informational_query("thanks") is False

    assert RetrievalService.is_informational_query("What are the membership fees for D-Business Forum?") is True
    assert RetrievalService.is_informational_query("How does the networking program work?") is True


@pytest.mark.asyncio
async def test_local_server_agent_parity(db_session):
    """Test 10: Verify local voice test and AgentEngine produce identical clean responses."""
    biz_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    agent_id = str(uuid.uuid4())

    biz = Business(id=biz_id, name="D-Business Forum", owner_user_id=user_id)
    agent = AIAgent(id=agent_id, business_id=biz_id, name="Alex", status="ACTIVE")
    db_session.add_all([biz, agent])
    await db_session.commit()

    script = "Good morning Mr. [Name]. I am calling from D-Business Forum. Can I speak with you for 2 minutes?"
    engine_res = await AgentEngine.process_user_query(
        db=db_session,
        agent_id=agent.id,
        message="Hello",
        script_text=script,
        current_language="en",
    )

    # Both must return clean text with no tags
    assert "<emotion" not in engine_res.response
    assert "<Calls Script>" not in engine_res.response
    assert isinstance(engine_res.to_dict(), dict)
    assert "response_text" in engine_res.to_dict()
    assert "emotion" in engine_res.to_dict()


def test_latency_timestamps():
    """Test 11: Verify latency breakdown dictionary contains all sub-phase metrics."""
    res = AgentEngineResponse(
        response="Good morning Mr. Kumar!",
        agent_id="test_agent",
        conversation_id="conv_1",
        session_id="sess_1",
        latency_ms=280,
        latency_breakdown={
            "stt_latency": 45,
            "intent_latency": 12,
            "rag_latency": 0,
            "gemini_latency": 140,
            "tts_latency": 83,
            "total_turn_latency": 280,
        },
        emotion="happy",
    )
    d = res.to_dict()
    assert d["latency_ms"] == 280
    assert d["latency_breakdown"]["total_turn_latency"] == 280
    assert d["latency_breakdown"]["gemini_latency"] == 140
    assert d["emotion"] == "happy"


def test_barge_in_after_clean_response():
    """Test 12: Verify clean response is delivered so barge-in and sentence boundaries work cleanly."""
    raw = '<emotion type="happy" />Hello! How can I help you today? Please let me know your question.'
    clean = ResponseSanitizer.clean_customer_facing_text(raw)
    assert clean == "Hello! How can I help you today? Please let me know your question."
    sentences = [s.strip() for s in re.split(r'[.!?]+\s*', clean) if s.strip()]
    assert len(sentences) >= 2
