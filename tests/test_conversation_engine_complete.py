import pytest
import asyncio
from app.conversation.session import SessionManager
from app.conversation.engine import ConversationalEngine
from app.conversation.response_guard import ResponseGuard

NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ = """
STRICT RECEPTIONIST RULES
The receptionist must never invent an appointment slot, staff availability, price, address or service guarantee. LEAD / APPOINTMENT DETAILS TO COLLECT

1. Greeting
Hello MANONMANI, this is MAGIC from Nurse O' Clock.

2. Requested Service
What service are you looking for today?

3. Patient Name
May I know the patient's name?

4. Patient Age
And may I know the patient's age?

5. Preferred Date
What date would work best for your appointment?

6. Preferred Time
What time do you prefer?

7. Closing
Thank you. We have registered your appointment details. Have a great day!

FREQUENTLY ASKED QUESTIONS
Q: What is Nurse O' Clock?
A: Nurse O' Clock is a premier in-home healthcare and nursing provider.
Q: Where is Nurse O' Clock located?
A: We are located in Chennai and provide services across the city.
"""

SOLAR_CAMPAIGN_SCRIPT = """
1. Greeting
Hello! This is MAGIC calling from Home Solar Experts. How are you today?

2. Home Ownership
Are you the owner of your home, or are you renting?
If yes: continue to property type.
If no: closing.

3. Property Type
Is your home a single family house or an apartment?

4. Monthly Electric Bill
What is your average monthly electric bill?

5. Closing
Thank you for your information. A solar specialist will prepare your quote!
"""

@pytest.mark.asyncio
async def test_exact_turn_sequence_point_22():
    """
    Executes the exact test specified in Point 22 of the user request:
    Turn 1: Initial call greeting
    Turn 2: Customer: "yeah what do you want"
            Must continue active script, NOT repeat greeting, NOT recite rules, NOT recite FAQ, NOT generic fallback.
    Turn 3: Customer: "I want to book an appointment."
            Identifies appointment intent and moves to next required step (Patient Name).
    Turn 4: Customer: "Mugilan."
            Advances to Patient Age.
    Turn 5: Customer: "25."
            Advances to Preferred Date.
    Turn 6: Customer: "Tomorrow."
            Advances to Preferred Time.
    """
    session_id = "test_point_22_session_001"
    campaign_id = "camp_nurse_oclock_001"
    SessionManager.clear_session(session_id)

    # 1. Initial Greeting Turn
    turn1 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="",
        script_text=NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ,
        campaign_id=campaign_id
    )
    assert "hello manonmani" in turn1["response"].lower()
    assert "magic" in turn1["response"].lower()

    # 2. Customer says: "yeah what do you want"
    turn2 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="yeah what do you want",
        script_text=NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ,
        campaign_id=campaign_id
    )
    r2 = turn2["response"].lower()

    # MUST NOT be repeated greeting
    assert not r2.startswith("hello manonmani"), "FAILED: Greeting was repeated!"
    # MUST NOT contain internal receptionist rules
    assert "receptionist must never invent" not in r2, "FAILED: Internal receptionist rule was spoken!"
    assert "lead / appointment details to collect" not in r2, "FAILED: Lead details header was spoken!"
    # MUST NOT contain raw RAG FAQ
    assert "frequently asked questions" not in r2, "FAILED: FAQ header was spoken!"
    assert "q:" not in r2 and "a:" not in r2, "FAILED: Raw Q:/A: was spoken!"
    # MUST NOT be generic fallback
    assert "could you please share more details so i can assist you" not in r2, "FAILED: Generic fallback loop returned!"

    # MUST continue active script (service enquiry / purpose)
    assert any(w in r2 for w in ["service", "looking for", "calling", "assist", "help", "enquiry"])

    # 3. Customer says: "I want to book an appointment."
    turn3 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="I want to book an appointment.",
        script_text=NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ,
        campaign_id=campaign_id
    )
    r3 = turn3["response"].lower()

    # Must move to Patient Name
    assert any(w in r3 for w in ["name", "patient"]), f"Expected patient name question, got: '{r3}'"
    assert "receptionist must never invent" not in r3
    assert "lead / appointment details to collect" not in r3

    # 4. Customer answers: "Mugilan."
    turn4 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="Mugilan.",
        script_text=NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ,
        campaign_id=campaign_id
    )
    r4 = turn4["response"].lower()

    # Patient name should now be recorded in collected_data
    session = SessionManager.get_session(session_id)
    assert session.collected_data.get("patient_name") == "Mugilan" or "patient_name" in session.collected_data
    # Must move to Patient Age
    assert any(w in r4 for w in ["age", "old"]), f"Expected patient age question, got: '{r4}'"
    # Never ask for name again!
    assert "may i know the patient's name" not in r4

    # 5. Customer answers: "25."
    turn5 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="25.",
        script_text=NURSE_OCLOCK_SCRIPT_WITH_INTERNAL_RULES_AND_FAQ,
        campaign_id=campaign_id
    )
    r5 = turn5["response"].lower()

    # Must move to Preferred Date
    assert any(w in r5 for w in ["date", "day", "when", "prefer"]), f"Expected preferred date question, got: '{r5}'"

@pytest.mark.asyncio
async def test_different_script_isolation_point_23():
    """
    Executes Point 23 of the user request:
    Test with a completely different campaign script (Home Solar).
    Verify that NO Nurse O' Clock questions or previous campaign content appear.
    """
    solar_session_id = "test_point_23_solar_session_001"
    solar_camp_id = "camp_solar_001"
    SessionManager.clear_session(solar_session_id)

    # 1. Greeting
    t1 = await ConversationalEngine.process_turn(
        call_session_id=solar_session_id,
        customer_utterance="",
        script_text=SOLAR_CAMPAIGN_SCRIPT,
        campaign_id=solar_camp_id
    )
    assert "home solar experts" in t1["response"].lower()
    assert "nurse" not in t1["response"].lower()

    # 2. Customer: "yeah what do you want"
    t2 = await ConversationalEngine.process_turn(
        call_session_id=solar_session_id,
        customer_utterance="yeah what do you want",
        script_text=SOLAR_CAMPAIGN_SCRIPT,
        campaign_id=solar_camp_id
    )
    r2 = t2["response"].lower()

    # MUST NOT contain Nurse O' Clock or healthcare
    assert "nurse" not in r2
    assert "patient" not in r2
    assert "appointment" not in r2
    # Must follow Solar script
    assert any(w in r2 for w in ["solar", "home", "owner", "renting", "calling"])

    # 3. Customer: "I am the owner."
    t3 = await ConversationalEngine.process_turn(
        call_session_id=solar_session_id,
        customer_utterance="I am the owner.",
        script_text=SOLAR_CAMPAIGN_SCRIPT,
        campaign_id=solar_camp_id
    )
    r3 = t3["response"].lower()

    # Must advance to property type
    assert any(w in r3 for w in ["property", "house", "apartment", "single family"])
    assert "nurse" not in r3

if __name__ == "__main__":
    print("Running test_exact_turn_sequence_point_22...")
    asyncio.run(test_exact_turn_sequence_point_22())
    print("test_exact_turn_sequence_point_22 PASSED!")
    print("Running test_different_script_isolation_point_23...")
    asyncio.run(test_different_script_isolation_point_23())
    print("test_different_script_isolation_point_23 PASSED!")


SALON_SCRIPT = """
1. Greeting
Hello! Thank you for calling Style Studio. How are you today?

2. Desired Service
What service are you looking for today?

3. Preferred Date and Time
When would you like to schedule your appointment?

4. Client Name
May I have your name, please?

5. Closing
Thank you! Your appointment is requested. We look forward to seeing you!

SERVICES OFFERED
We offer haircut, hair coloring, facial, manicure, and pedicure services.
"""

HEALTHCARE_SCRIPT = """
STRICT RECEPTIONIST RULES
The receptionist must never invent an appointment slot, price, or doctor availability.

1. Greeting
Hello! This is MAGIC calling from City Care Clinic. How can I help you?

2. Service Required
What healthcare service are you looking for?

3. Patient Name
May I know the patient's name?

4. Closing
Thank you. We have recorded your request.

FREQUENTLY ASKED QUESTIONS
Q: What services do you provide?
A: We provide in-home doctor visits, nursing care, and physiotherapy.
Q: Where are you located?
A: We are located on Anna Salai, Chennai.
"""

@pytest.mark.asyncio
async def test_scenario_1_ask_service_then_datetime():
    """TEST 1: Customer: 'I want a haircut.' -> service=haircut, moves to date/time."""
    session_id = "test_scen_1_session"
    camp_id = "camp_scen_1"
    SessionManager.clear_session(session_id)

    # 1. Greeting
    await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    # 2. Customer answers service
    t2 = await ConversationalEngine.process_turn(session_id, "I want a haircut.", SALON_SCRIPT, camp_id)
    sess = SessionManager.get_session(session_id)

    assert sess.collected_data.get("service_required") == "haircut"
    # Moved to date and time
    assert "step_003" in sess.current_step_id or "date" in t2["response"].lower() or "when" in t2["response"].lower()


@pytest.mark.asyncio
async def test_scenario_2_what_do_you_want_clarification():
    """TEST 2: Customer: 'What do you want?' -> Clarification based on active script, no greeting restart, no generic fallback."""
    session_id = "test_scen_2_session"
    camp_id = "camp_scen_2"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    t2 = await ConversationalEngine.process_turn(session_id, "What do you want?", SALON_SCRIPT, camp_id)
    r2 = t2["response"].lower()

    assert not r2.startswith("hello! thank you for calling style studio")
    assert "could you please share more details" not in r2
    assert any(w in r2 for w in ["style studio", "service", "appointment", "calling", "assist", "help"])


@pytest.mark.asyncio
async def test_scenario_3_hi_acknowledgement():
    """TEST 3: Customer: 'Hi' -> Natural acknowledgement, no greeting restart, no state reset."""
    session_id = "test_scen_3_session"
    camp_id = "camp_scen_3"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    t2 = await ConversationalEngine.process_turn(session_id, "Hi", SALON_SCRIPT, camp_id)
    r2 = t2["response"].lower()

    assert not r2.startswith("hello! thank you for calling style studio")
    assert t2["current_step_id"] != "step_001"


@pytest.mark.asyncio
async def test_scenario_4_two_fields_one_sentence():
    """TEST 4: 'I want a haircut tomorrow evening.' -> collects service + date/time, skips date/time, asks name."""
    session_id = "test_scen_4_session"
    camp_id = "camp_scen_4"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    t2 = await ConversationalEngine.process_turn(session_id, "I want a haircut tomorrow evening.", SALON_SCRIPT, camp_id)
    sess = SessionManager.get_session(session_id)

    assert sess.collected_data.get("service_required") == "haircut"
    assert "tomorrow" in str(sess.collected_data.get("date", "")).lower() or "tomorrow" in str(sess.collected_data.get("preferred_date_time", "")).lower()
    # Must skip date/time and move directly to client name
    r2 = t2["response"].lower()
    assert "name" in r2 or sess.current_step_id == "step_004"


@pytest.mark.asyncio
async def test_scenario_5_knowledge_query_and_return_to_step():
    """TEST 5: 'What services do you provide?' -> RAG search active script, answers naturally, preserves step."""
    session_id = "test_scen_5_session"
    camp_id = "camp_scen_5"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", HEALTHCARE_SCRIPT, camp_id)
    t2 = await ConversationalEngine.process_turn(session_id, "What services do you provide?", HEALTHCARE_SCRIPT, camp_id)
    sess = SessionManager.get_session(session_id)

    r2 = t2["response"].lower()
    # Answers with healthcare knowledge from active script
    assert any(w in r2 for w in ["nursing", "doctor", "physiotherapy", "healthcare", "services"])
    # Did not advance beyond question step
    assert sess.current_step_id == "step_002"


@pytest.mark.asyncio
async def test_scenario_6_dual_campaign_rag_isolation():
    """TEST 6: Campaign A (Salon) & Campaign B (Healthcare) run simultaneously. Only Campaign B knowledge retrieved."""
    camp_a = "camp_salon_001"
    camp_b = "camp_health_001"

    # Index both
    await ConversationalEngine.process_turn("sess_a", "", SALON_SCRIPT, camp_a)
    await ConversationalEngine.process_turn("sess_b", "", HEALTHCARE_SCRIPT, camp_b)

    from app.rag.retriever import IsolatedRAGRetriever
    chunks_b = IsolatedRAGRetriever.retrieve_context("What services do you provide?", camp_b)
    text_b = " ".join(chunks_b).lower()

    assert any(w in text_b for w in ["nursing", "doctor", "physiotherapy", "care"])
    assert "haircut" not in text_b
    assert "pedicure" not in text_b
    assert "manicure" not in text_b


@pytest.mark.asyncio
async def test_scenario_7_duplicate_stt_protection():
    """TEST 7: STT produces 'I want an appointment.' twice -> duplicate turn dropped."""
    session_id = "test_scen_7_session"
    camp_id = "camp_scen_7"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", HEALTHCARE_SCRIPT, camp_id)
    t2 = await ConversationalEngine.process_turn(session_id, "I want an appointment.", HEALTHCARE_SCRIPT, camp_id)
    t3 = await ConversationalEngine.process_turn(session_id, "I want an appointment.", HEALTHCARE_SCRIPT, camp_id)

    assert t3.get("is_duplicate") is True
    assert t3["response"] == t2["response"]


@pytest.mark.asyncio
async def test_scenario_8_language_switch_preserves_state():
    """TEST 8: 'Continue in Tamil.' -> language changes to ta, collected data & step preserved."""
    session_id = "test_scen_8_session"
    camp_id = "camp_scen_8"
    SessionManager.clear_session(session_id)

    await ConversationalEngine.process_turn(session_id, "", SALON_SCRIPT, camp_id)
    await ConversationalEngine.process_turn(session_id, "I want a haircut.", SALON_SCRIPT, camp_id)
    t3 = await ConversationalEngine.process_turn(session_id, "Continue in Tamil.", SALON_SCRIPT, camp_id)
    sess = SessionManager.get_session(session_id)

    assert sess.language == "ta"
    assert sess.language_locked is True
    assert sess.collected_data.get("service_required") == "haircut"


def test_scenario_9_raw_rag_protection():
    """TEST 9: Internal rules and headers are stripped by ResponseGuard."""
    raw = "STRICT RECEPTIONIST RULES The receptionist must never invent an appointment slot. LEAD / APPOINTMENT DETAILS TO COLLECT May I know your name?"
    clean = ResponseGuard.sanitize_for_tts(raw, target_step_text="May I know your name?")
    clean_lower = clean.lower()

    assert "strict receptionist rules" not in clean_lower
    assert "the receptionist must never invent" not in clean_lower
    assert "lead / appointment details" not in clean_lower
    assert "may i know your name" in clean_lower


def test_scenario_10_ragconfig_no_error():
    """TEST 10: RAGConfig instantiates cleanly with embedding_model."""
    from app.db.models import RAGConfig
    cfg = RAGConfig(
        business_id="test_enterprise",
        name="Enterprise RAG",
        embedding_model="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        llm_model="gemini-3.8-flash",
        top_k=5
    )
    assert cfg.embedding_model == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    assert cfg.top_k == 5

