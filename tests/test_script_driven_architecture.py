import pytest
import asyncio

from app.script_engine.parser import ScriptParser
from app.script_engine.flow_engine import ScriptFlowEngine
from app.conversation.session import SessionManager
from app.conversation.language import LanguageManager
from app.conversation.response_guard import ResponseGuard
from app.conversation.engine import ConversationalEngine

SAMPLE_SCRIPT = """
Business: ABC Health Services
1. Greeting
Hello! My name is Alex calling from ABC Health Services. How are you today?

2. Service Inquiry
Are you interested in booking a healthcare appointment with us?
If yes: continue to service selection.
If no: closing.

3. Service Type
Which specific service are you looking for?

4. Preferred Date
What date would work best for your appointment?

5. Preferred Time
What time of day do you prefer?

6. Closing
Thank you for your time. We have registered your request. Have a great day!
"""

@pytest.mark.asyncio
async def test_script_driven_consecutive_flow():
    session_id = "test_script_flow_session_001"
    campaign_id = "camp_abc_01"
    SessionManager.clear_session(session_id)

    # Turn 1: Initial call response (Greeting)
    res1 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="",
        script_text=SAMPLE_SCRIPT,
        campaign_id=campaign_id
    )
    assert "abc health services" in res1["response"].lower()

    # Turn 2: Customer answers "hello" -> Advances to Question 1
    res2 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="hello",
        script_text=SAMPLE_SCRIPT,
        campaign_id=campaign_id
    )
    assert "booking a healthcare appointment" in res2["response"].lower() or "interested" in res2["response"].lower()

    # Turn 3: Customer says "yes" -> Advances to Question 2
    res3 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="yes",
        script_text=SAMPLE_SCRIPT,
        campaign_id=campaign_id
    )
    assert "which specific service" in res3["response"].lower() or "service" in res3["response"].lower()

    # Turn 3: Customer switches language to Tamil
    res3 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="seri tamil la pesuviya",
        script_text=SAMPLE_SCRIPT,
        campaign_id=campaign_id
    )
    assert res3["language"] == "ta"

    # Turn 4: Customer explicitly switches language back to English
    res4 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="continue in English",
        script_text=SAMPLE_SCRIPT,
        campaign_id=campaign_id
    )
    assert res4["language"] == "en"
    assert "service" in res4["response"].lower() or "time" in res4["response"].lower() or "prefer" in res4["response"].lower() or "date" in res4["response"].lower()

@pytest.mark.asyncio
async def test_response_guard_strips_internal_instructions():
    raw_workflow = "Identify the requested service. What date would work best?"
    sanitized = ResponseGuard.sanitize_for_tts(raw_workflow)
    assert "identify the requested service" not in sanitized.lower()
    assert "what date would work best" in sanitized.lower()
