import pytest
import asyncio
import json
import sqlite3

from app.conversation.engine import ConversationalEngine
from app.conversation.session import SessionManager
from app.script_engine.parser import ScriptParser

NURSE_OCLOCK_SCRIPT = """
NURSE O' CLOCK – CHENNAI INFORMATION
Organization Name: Nurse O' Clock
Organization Type: Home Healthcare and Patient-Care Service Provider
Service Location: Chennai, Tamil Nadu

SERVICES
Senior Assisted Living: Support for senior citizens who need assistance with daily living.
Nursing Services: Professional nursing support for patients who require nursing care at home.
Home Nurse / Medical Assistants: Home-based assistance for patients who require medical care.
Elderly Care: Dedicated care and assistance for senior citizens at home.
Physiotherapy: Home-based physiotherapy support for physical rehabilitation.

DETAILS TO COLLECT
1. Patient Name
2. Patient Age
3. Service Required
4. Preferred Date
5. Preferred Time

STANDARD AI RECEPTIONIST CONVERSATION FLOW
1. Greet the caller warmly.
2. Identify intent: service enquiry or appointment request.
3. Ask who needs care and collect only relevant basic details.
4. Ask for preferred date and time.
5. Confirm the information collected.
6. Close politely and empathetically.

FREQUENTLY ASKED QUESTIONS
Q: Can you tell me what services you provide?
A: We provide in-home nursing services, physiotherapy, doctor visits, and elderly care across Chennai.
"""

SOLAR_CAMPAIGN_SCRIPT = """
1. Greeting
Hello! This is MAGIC calling from SunPower Solar. How are you today?

2. Home Ownership
Do you own or rent your home?

3. Monthly Electricity Bill
What is your average monthly electricity bill?

4. Location
Which locality or area are you located in?

5. Roof Type
What type of roof does your home have?

6. Preferred Consultation Date
What date would work best for a free solar consultation?

7. Confirmation
Let me confirm the details we have noted.

8. Closing
Thank you for your time. Have a wonderful day!
"""

AUTO_TRADEIN_SCRIPT = """
1. Greeting
Hello! This is MAGIC calling from Apex Motors. How are you today?

2. Vehicle Make and Model
What is the make and model of the vehicle you want to trade in?

3. Year of Manufacture
What year was your vehicle manufactured?

4. Current Mileage
What is the current mileage on the odometer?

5. Expected Trade-In Value
What is your expected trade-in value?

6. Confirmation
Let me confirm the details we have noted.

7. Closing
Thank you for your time. Our trade-in specialist will contact you shortly!
"""

@pytest.mark.asyncio
async def test_point_20_exact_nurse_oclock_conversation():
    """
    Executes the exact conceptual conversation from Point 20:
    AI: 'Good Afternoon ... How are you doing today?'
    Customer: 'I am doing good, how about you?' -> Next step (Service)
    Customer: 'I want to book an appointment.' -> Identifies appointment intent, moves to Patient Name
    Customer: 'what details do you need?' -> Clarification, stays on Patient Name, does not close or advance
    Customer: 'My name is Ravi.' -> Stores Ravi, advances to Patient Age
    Customer: 'I am 35.' -> Stores 35, advances to Date & Time
    Customer: 'I want tomorrow.' -> Stores Tomorrow
    Customer: 'Can you tell me what services you provide?' -> RAG answers, returns to active step
    Customer: 'at 10 am' -> Stores time, advances to Confirmation
    Customer: 'Yes, that is correct.' -> Confirmation completes, advances to Closing
    """
    session_id = "test_point_20_sess_001"
    campaign_id = "camp_nurse_pt20"
    SessionManager.clear_session(session_id)

    # 1. AI Greeting Turn
    print("\n--- TURN 1: AI Greeting ---")
    t1 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r1 = t1["response"]
    print("AI 1:", r1)
    assert t1["current_step_id"] == "step_002"
    assert not t1["is_complete"]

    # 2. Customer: "I am doing good, how about you?"
    print("\n--- TURN 2: Customer 'I am doing good, how about you?' ---")
    t2 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="I am doing good, how about you?",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r2 = t2["response"]
    print("AI 2:", r2)
    # MUST continue according to NEXT step (Service Requirement), NOT jump to age!
    assert "age" not in r2.lower()
    assert "doing" not in str(t2["collected_data"])  # Must NOT extract "doing" as name
    assert t2["current_step_id"] == "step_002"  # Stays on Service Requirement
    assert not t2["is_complete"]

    # 3. Customer: "I want to book an appointment."
    print("\n--- TURN 3: Customer 'I want to book an appointment.' ---")
    t3 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="I want to book an appointment.",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r3 = t3["response"]
    print("AI 3:", r3)
    # Identifies appointment intent, advances to Patient Name
    assert t3["current_step_id"] == "step_003"
    assert "name" in r3.lower() or "patient" in r3.lower()
    assert not t3["is_complete"]

    # 4. Customer: "what details do you need?"
    print("\n--- TURN 4: Customer 'what details do you need?' ---")
    t4 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="what details do you need?",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r4 = t4["response"]
    print("AI 4:", r4)
    # Clarification must NOT advance or close!
    assert t4["current_step_id"] == "step_003"  # Stays on Patient Name
    assert not t4["is_complete"]
    assert "thank you for your time" not in r4.lower()
    assert "have a wonderful day" not in r4.lower()

    # 5. Customer: "My name is Ravi."
    print("\n--- TURN 5: Customer 'My name is Ravi.' ---")
    t5 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="My name is Ravi.",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r5 = t5["response"]
    print("AI 5:", r5)
    assert "Ravi" in str(t5["collected_data"])
    assert t5["current_step_id"] == "step_004"  # Advances to Patient Age
    assert "age" in r5.lower() or "old" in r5.lower()
    assert not t5["is_complete"]

    # 6. Customer: "I am 35."
    print("\n--- TURN 6: Customer 'I am 35.' ---")
    t6 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="I am 35.",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r6 = t6["response"]
    print("AI 6:", r6)
    assert "35" in str(t6["collected_data"])
    assert t6["current_step_id"] == "step_005"  # Advances to Preferred Date & Time
    assert "date" in r6.lower() or "time" in r6.lower()
    assert not t6["is_complete"]

    # 7. Customer: "I want tomorrow."
    print("\n--- TURN 7: Customer 'I want tomorrow.' ---")
    t7 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="I want tomorrow.",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r7 = t7["response"]
    print("AI 7:", r7)
    assert "Tomorrow" in str(t7["collected_data"])
    assert not t7["is_complete"]

    # 8. Customer asks RAG question: "Can you tell me what services you provide?"
    print("\n--- TURN 8: Customer 'Can you tell me what services you provide?' ---")
    t8 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="Can you tell me what services you provide?",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r8 = t8["response"]
    print("AI 8:", r8)
    # Must NOT restart greeting
    assert not r8.lower().startswith("hello! how can i")
    # Must NOT close the call
    assert not t8["is_complete"]
    assert "thank you for your time" not in r8.lower()
    # Must NOT dump raw document headers
    assert "NURSE O' CLOCK – CHENNAI INFORMATION" not in r8
    assert "STRICT RECEPTIONIST RULES" not in r8
    assert any(w in r8.lower() for w in ["nursing", "physiotherapy", "care", "service"])

    # 9. Customer provides time: "at 10 am"
    print("\n--- TURN 9: Customer 'at 10 am' ---")
    t9 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="at 10 am",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r9 = t9["response"]
    print("AI 9:", r9)
    # Date & time complete -> moves to Confirmation
    assert any(w in r9.lower() for w in ["confirm", "information", "details"])
    assert not t9["is_complete"]

    # 10. Customer confirms: "Yes, that is correct."
    print("\n--- TURN 10: Customer 'Yes, that is correct.' ---")
    t10 = await ConversationalEngine.process_turn(
        call_session_id=session_id,
        customer_utterance="Yes, that is correct.",
        script_text=NURSE_OCLOCK_SCRIPT,
        campaign_id=campaign_id
    )
    r10 = t10["response"]
    print("AI 10:", r10)
    # Confirmation complete -> Closing!
    assert any(w in r10.lower() for w in ["thank you", "wonderful day", "great day", "day"])
    assert t10["is_complete"]


@pytest.mark.asyncio
async def test_point_21_dynamic_solar_campaign_zero_code_changes():
    """
    Executes Point 21: Completely different Solar Campaign script with different questions.
    Verifies that the engine dynamically discovers and asks all Solar questions with ZERO code changes:
    1. Greeting
    2. Home ownership (own or rent)
    3. Monthly electricity bill
    4. Location
    5. Roof type
    6. Consultation date
    7. Confirmation
    8. Closing
    """
    session_id = "test_point_21_solar_001"
    campaign_id = "camp_solar_pt21"
    SessionManager.clear_session(session_id)

    # 1. Greeting
    t1 = await ConversationalEngine.process_turn(session_id, "", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "sunpower solar" in t1["response"].lower()
    assert t1["current_step_id"] == "step_002"

    # 2. Owns or rents
    t2 = await ConversationalEngine.process_turn(session_id, "Hello, yes I am doing good.", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert any(w in t2["response"].lower() for w in ["own", "rent", "home"])
    assert t2["current_step_id"] == "step_002"

    # Provide ownership
    t3 = await ConversationalEngine.process_turn(session_id, "I own my home.", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "owner" in str(t3["collected_data"]).lower()
    assert t3["current_step_id"] == "step_003"
    assert any(w in t3["response"].lower() for w in ["bill", "electricity", "electric"])

    # Provide bill
    t4 = await ConversationalEngine.process_turn(session_id, "$180 per month.", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "180" in str(t4["collected_data"])
    assert t4["current_step_id"] == "step_004"
    assert any(w in t4["response"].lower() for w in ["location", "area", "locality"])

    # Provide location
    t5 = await ConversationalEngine.process_turn(session_id, "Austin Texas", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "Austin" in str(t5["collected_data"])
    assert t5["current_step_id"] == "step_005"
    assert any(w in t5["response"].lower() for w in ["roof", "shingle", "tile"])

    # Provide roof type
    t6 = await ConversationalEngine.process_turn(session_id, "Shingle roof", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "shingle" in str(t6["collected_data"]).lower()
    assert t6["current_step_id"] == "step_006"
    assert any(w in t6["response"].lower() for w in ["date", "consultation", "work best"])

    # Provide consultation date
    t7 = await ConversationalEngine.process_turn(session_id, "Next Monday", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert "Monday" in str(t7["collected_data"])
    assert t7["current_step_id"] == "step_007"
    assert any(w in t7["response"].lower() for w in ["confirm", "noted", "details"])

    # Confirm
    t8 = await ConversationalEngine.process_turn(session_id, "Yes, that is right.", SOLAR_CAMPAIGN_SCRIPT, campaign_id)
    assert t8["is_complete"]
    assert any(w in t8["response"].lower() for w in ["thank you", "wonderful day", "have a"])


@pytest.mark.asyncio
async def test_point_21_dynamic_auto_tradein_campaign_zero_code_changes():
    """
    Executes a third unrelated script (Auto Trade-In) with ZERO code changes.
    Questions:
    1. Greeting
    2. Vehicle Make and Model
    3. Year of Manufacture
    4. Current Mileage
    5. Expected Trade-In Value
    6. Confirmation
    7. Closing
    """
    session_id = "test_point_21_auto_001"
    campaign_id = "camp_auto_pt21"
    SessionManager.clear_session(session_id)

    # 1. Greeting
    t1 = await ConversationalEngine.process_turn(session_id, "", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert "apex motors" in t1["response"].lower()
    assert t1["current_step_id"] == "step_002"

    # 2. Make and Model
    t2 = await ConversationalEngine.process_turn(session_id, "Hi, I am fine.", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert any(w in t2["response"].lower() for w in ["make", "model", "vehicle", "car"])
    assert t2["current_step_id"] == "step_002"

    # Provide Make and Model
    t3 = await ConversationalEngine.process_turn(session_id, "Honda Civic", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert t3["current_step_id"] == "step_003"
    assert any(w in t3["response"].lower() for w in ["year", "manufactured"])

    # Provide Year
    t4 = await ConversationalEngine.process_turn(session_id, "2020", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert t4["current_step_id"] == "step_004"
    assert any(w in t4["response"].lower() for w in ["mileage", "odometer"])

    # Provide Mileage
    t5 = await ConversationalEngine.process_turn(session_id, "45000 miles", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert t5["current_step_id"] == "step_005"
    assert any(w in t5["response"].lower() for w in ["expected", "value", "price", "trade-in"])

    # Provide Expected Value
    t6 = await ConversationalEngine.process_turn(session_id, "$18000", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert t6["current_step_id"] == "step_006"
    assert any(w in t6["response"].lower() for w in ["confirm", "noted", "details"])

    # Confirm
    t7 = await ConversationalEngine.process_turn(session_id, "Yes, confirm please.", AUTO_TRADEIN_SCRIPT, campaign_id)
    assert t7["is_complete"]
    assert any(w in t7["response"].lower() for w in ["thank you", "specialist", "contact you"])
