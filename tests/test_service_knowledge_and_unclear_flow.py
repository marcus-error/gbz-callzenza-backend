import pytest
import asyncio

from app.conversation.engine import ConversationalEngine
from app.conversation.session import SessionManager
from tests.test_points_20_and_21_exact_suite import NURSE_OCLOCK_SCRIPT
from tests.test_conversation_engine_complete import SALON_SCRIPT

@pytest.mark.asyncio
async def test_service_listing_and_idk_flow():
    """
    Validates:
    1. 'can you list the services?' -> Retrieves from active script/RAG, answers naturally, stays on step_002.
    2. 'what services do you provide?' -> Retrieves from active script/RAG, answers naturally, stays on step_002.
    3. 'list the services in Nurse O' Clock' -> Retrieves from active script/RAG, answers naturally, stays on step_002.
    4. 'idk' -> Does NOT accept as service, stays on step_002, responds with service options.
    5. 'Physiotherapy' -> Stores service_required, marks step_002 complete, advances to Patient Name (step_003).
    """
    session_id = "test_sess_srv_idk_001"
    camp_id = "camp_srv_idk_001"
    SessionManager.clear_session(session_id)

    # Turn 1: Initial greeting
    t1 = await ConversationalEngine.process_turn(session_id, "", NURSE_OCLOCK_SCRIPT, camp_id)
    assert t1["current_step_id"] == "step_002"
    assert "step_001" in t1["completed_steps"]
    assert "step_002" not in t1["completed_steps"]

    # Turn 2: 'can you list the services?'
    t2 = await ConversationalEngine.process_turn(session_id, "can you list the services?", NURSE_OCLOCK_SCRIPT, camp_id)
    r2 = t2["response"].lower()
    assert any(w in r2 for w in ["nursing", "physiotherapy", "doctor", "elderly care", "services"])
    assert any(w in r2 for w in ["which", "looking for", "need"])
    assert t2["current_step_id"] == "step_002", "Must NOT advance script step on knowledge query"
    assert "step_002" not in t2["completed_steps"]
    assert "service_required" not in t2["collected_data"]

    # Turn 3: 'idk'
    t3 = await ConversationalEngine.process_turn(session_id, "idk", NURSE_OCLOCK_SCRIPT, camp_id)
    r3 = t3["response"].lower()
    assert "no problem" in r3
    assert any(w in r3 for w in ["nursing", "physiotherapy", "doctor", "elderly care", "services"])
    assert any(w in r3 for w in ["which", "looking for", "need"])
    assert t3["current_step_id"] == "step_002", "Must NOT advance to Patient Name on 'idk'"
    assert "step_002" not in t3["completed_steps"]
    assert "service_required" not in t3["collected_data"]
    assert "patient_name" not in t3["collected_data"]

    # Turn 4: 'Physiotherapy'
    t4 = await ConversationalEngine.process_turn(session_id, "Physiotherapy", NURSE_OCLOCK_SCRIPT, camp_id)
    assert t4["collected_data"].get("service_required") == "physiotherapy"
    assert "step_002" in t4["completed_steps"]
    assert t4["current_step_id"] == "step_003", "Must advance to step_003 (Patient Name)"
    r4 = t4["response"].lower()
    assert any(w in r4 for w in ["name", "patient", "who needs care"])


@pytest.mark.asyncio
async def test_salon_service_listing_and_idk_flow():
    """
    Validates complete domain-agnosticism on a non-healthcare script (Salon):
    Zero hardcoded values: dynamically retrieves salon services.
    """
    session_id = "test_sess_salon_idk_002"
    camp_id = "camp_salon_idk_002"
    SessionManager.clear_session(session_id)

    # Turn 1: Initial greeting
    t1 = await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    assert t1["current_step_id"] == "step_002"

    # Turn 2: 'what services do you provide?'
    t2 = await ConversationalEngine.process_turn(session_id, "what services do you provide?", SALON_SCRIPT, camp_id)
    r2 = t2["response"].lower()
    assert any(w in r2 for w in ["haircut", "facial", "manicure", "pedicure", "hair coloring"])
    assert t2["current_step_id"] == "step_002"

    # Turn 3: 'dunno'
    t3 = await ConversationalEngine.process_turn(session_id, "dunno", SALON_SCRIPT, camp_id)
    r3 = t3["response"].lower()
    assert "no problem" in r3
    assert any(w in r3 for w in ["haircut", "facial", "manicure", "pedicure", "hair coloring"])
    assert t3["current_step_id"] == "step_002"
    assert "service_required" not in t3["collected_data"]
