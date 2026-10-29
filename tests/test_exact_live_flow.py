import asyncio
import json
import sqlite3
import pytest

from app.conversation.engine import ConversationalEngine
from app.conversation.session import SessionManager
from app.script_engine.parser import ScriptParser
from app.script_engine.flow_engine import ScriptFlowEngine

@pytest.mark.asyncio
async def test_exact_nurse_o_clock_live_conversation():
    # 1. Fetch real campaign script from callzenza.db
    con = sqlite3.connect("callzenza.db")
    cur = con.cursor()
    row = cur.execute("SELECT schedule FROM campaigns LIMIT 1").fetchone()
    data = json.loads(row[0])
    script_text = data.get("script")
    assert script_text is not None

    call_id = "test_live_call_exact_001"
    campaign_id = "1b720714-520e-419a-9084-ea799a30c47a"
    opening_line = "Good Morning MANONMANI Viswanathan! Hi, this is MAGIC calling from Nurse O' Clock. How are you doing today?"

    # Simulate Call Answer & Greeting sent over RTP by sip_ai_bot
    conv_session = SessionManager.get_or_create_session(
        call_session_id=call_id,
        campaign_id=campaign_id
    )
    conv_session.greeting_sent = True
    conv_session.turn_id = 1
    conv_session.last_ai_response = opening_line
    conv_session.conversation_history.append({"role": "assistant", "content": opening_line})

    flow = ScriptParser.parse_script(script_text=script_text, campaign_id=campaign_id)
    first_step = flow.get_first_step()
    assert first_step is not None
    assert first_step.title == "Greeting"
    assert "NURSE O' CLOCK – CHENNAI INFORMATION" not in first_step.question_or_text
    conv_session.completed_steps.append(first_step.step_id)
    next_step, _ = ScriptFlowEngine.get_next_step(
        flow=flow,
        current_step=first_step,
        completed_step_ids=conv_session.completed_steps,
        collected_data=conv_session.collected_data
    )
    assert next_step is not None
    conv_session.current_step_id = next_step.step_id

    # TURN 1: Customer says "hi"
    print("\n--- TURN 1: CUSTOMER says 'hi' ---")
    t1 = await ConversationalEngine.process_turn(
        call_session_id=call_id,
        customer_utterance="hi",
        script_text=script_text,
        campaign_id=campaign_id
    )
    resp1 = t1["response"]
    print("AI RESPONSE 1:", resp1)

    # Assertions for Turn 1
    assert "NURSE O' CLOCK – CHENNAI INFORMATION" not in resp1
    assert "Organization Name:" not in resp1
    assert "SERVICES" not in resp1
    assert "OFFICE / SERVICE CENTER INFORMATION" not in resp1
    assert "Good Morning MANONMANI" not in resp1  # Must not replay greeting
    assert len(resp1.split()) < 40  # Must be a short spoken response
    assert t1["current_step_id"] == "step_002"  # Stays at Service Requirement
    assert "step_002" not in t1["completed_steps"]  # Step not completed by just "hi"

    # TURN 2: Customer says "i want to book an appoitment"
    print("\n--- TURN 2: CUSTOMER says 'i want to book an appoitment' ---")
    t2 = await ConversationalEngine.process_turn(
        call_session_id=call_id,
        customer_utterance="i want to book an appoitment",
        script_text=script_text,
        campaign_id=campaign_id
    )
    resp2 = t2["response"]
    print("AI RESPONSE 2:", resp2)

    # Assertions for Turn 2
    assert "NURSE O' CLOCK – CHENNAI INFORMATION" not in resp2
    assert "step_002" in t2["completed_steps"]  # Service requirement completed!
    assert t2["current_step_id"] == "step_003"  # Advanced to Patient Name!
    assert "appointment" in str(t2["collected_data"]).lower()  # Extracted service/appointment
    assert any(w in resp2.lower() for w in ["name", "patient", "appointment", "help"])

    # TURN 3: Customer says "yes"
    print("\n--- TURN 3: CUSTOMER says 'yes' ---")
    t3 = await ConversationalEngine.process_turn(
        call_session_id=call_id,
        customer_utterance="yes",
        script_text=script_text,
        campaign_id=campaign_id
    )
    resp3 = t3["response"]
    print("AI RESPONSE 3:", resp3)

    # Assertions for Turn 3
    assert "NURSE O' CLOCK – CHENNAI INFORMATION" not in resp3
    assert len(resp3) > 5
    assert not resp3.startswith("yes")
    # Patient name was not provided, so AI must prompt for patient details
    assert t3["current_step_id"] == "step_003"  # Stays at Patient Name until name provided
    assert any(w in resp3.lower() for w in ["name", "patient", "share", "provide", "proceed"])

    # TURN 4: Customer provides patient name "Mugilan"
    print("\n--- TURN 4: CUSTOMER says 'Mugilan' ---")
    t4 = await ConversationalEngine.process_turn(
        call_session_id=call_id,
        customer_utterance="Mugilan",
        script_text=script_text,
        campaign_id=campaign_id
    )
    resp4 = t4["response"]
    print("AI RESPONSE 4:", resp4)
    assert "step_003" in t4["completed_steps"]
    assert t4["current_step_id"] == "step_004"  # Advanced to Patient Age!
    assert "Mugilan" in str(t4["collected_data"])
    assert any(w in resp4.lower() for w in ["age", "old"])
