"""
Unit and Integration Tests for Dynamic Script-Driven Qualification Flow, Field Extraction,
Script Fast-Reply Guard, Language Switching, and Multi-Domain Script/RAG Isolation.
"""
import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.ai.qualification_service import QualificationStageEngine
from app.services.language_service import detect_explicit_language_switch_request, detect_speech_language


def test_scenario_1_service_name_date_location():
    """Agent A has Service -> Name -> Date -> Location."""
    script_a = (
        "First understand what service the customer requires.\n"
        "Then collect the customer's name.\n"
        "Ask their preferred appointment date.\n"
        "Ask their location in the city.\n"
        "Finally confirm the appointment."
    )
    stages = QualificationStageEngine.extract_stages_from_script(script_a, agent_name="NurseAgent", business_name="CareHealth")
    assert len(stages) >= 4
    
    # Verify stages have correct field mappings and titles
    field_keys = [s["field"] for s in stages]
    assert "service" in field_keys or "requirement" in field_keys
    assert "name" in field_keys
    assert "date" in field_keys
    assert "location" in field_keys

    # Initial evaluation: first stage should be CURRENT
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, {}, 0)
    assert steps[0]["status"] == "CURRENT"
    assert status == "IN_PROGRESS"
    assert len(missing) >= 3

    # Customer provides service
    collected = {"service": "Home Nurse"}
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, collected, 0)
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "CURRENT"


def test_scenario_2_requirement_budget_location_availability():
    """Agent B has Requirement -> Budget -> Location -> Availability."""
    script_b = "Requirement -> Budget -> Location -> Availability -> Schedule Visit"
    stages = QualificationStageEngine.extract_stages_from_script(script_b, agent_name="RealEstateAgent", business_name="PrimeProperties")
    assert len(stages) == 5
    
    stage_names = [s["name"].lower() for s in stages]
    assert "requirement" in stage_names[0]
    assert "budget" in stage_names[1]
    assert "location" in stage_names[2]
    assert "availability" in stage_names[3]

    # Progressive collection
    collected = {"requirement": "3BHK Villa", "budget": "1.5 Crore"}
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, collected, 0)
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "DONE"
    assert steps[2]["status"] == "CURRENT"
    assert steps[3]["status"] == "PENDING"
    assert next_f is not None and "location" in next_f.lower()


def test_scenario_3_multi_field_single_utterance():
    """Customer provides multiple fields in one sentence: 'I need a home nurse for my mother tomorrow at Anna Nagar. My name is Kumar.'"""
    script = (
        "1. Identify requested service\n"
        "2. Collect customer name\n"
        "3. Preferred appointment date\n"
        "4. Customer location\n"
        "5. Confirmation"
    )
    stages = QualificationStageEngine.extract_stages_from_script(script, agent_name="CareBot", business_name="HealthHome")
    
    utterance = "I need a home nurse for my mother tomorrow at Anna Nagar. My name is Kumar."
    collected = QualificationStageEngine.extract_entities_from_text(utterance)
    
    assert collected.get("service") in ("home nurse", "Home Nurse", "nurse") or "home nurse" in str(collected.get("service", "")).lower()
    assert collected.get("name") == "Kumar"
    assert collected.get("date") == "tomorrow"
    assert collected.get("location") == "Anna Nagar"

    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, collected, 0)
    
    # All 4 requirement stages should be marked DONE simultaneously
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "DONE"
    assert steps[2]["status"] == "DONE"
    assert steps[3]["status"] == "DONE"
    assert active_idx >= 4 or status == "QUALIFIED"


def test_scenario_4_out_of_order_answers():
    """Customer provides location first, then date, then service, then name."""
    script = (
        "1. Service Requirement\n"
        "2. Contact Name\n"
        "3. Appointment Date\n"
        "4. Area Location"
    )
    stages = QualificationStageEngine.extract_stages_from_script(script)
    
    # Turn 1: Customer gives Location
    t1 = QualificationStageEngine.extract_entities_from_text("I am located in Velachery")
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, t1, 0)
    assert steps[3]["status"] == "DONE"  # Area Location satisfied
    assert steps[0]["status"] == "CURRENT"  # Service Requirement still needed

    # Turn 2: Customer gives Date
    t2 = QualificationStageEngine.extract_entities_from_text("Can you come next Monday?", existing_fields=t1)
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, t2, 0)
    assert steps[2]["status"] == "DONE"  # Date satisfied
    assert steps[3]["status"] == "DONE"  # Location satisfied
    assert steps[0]["status"] == "CURRENT"  # Service Requirement still needed


def test_scenario_5_customer_changes_earlier_answer():
    """Customer changes earlier appointment date or name."""
    t1 = QualificationStageEngine.extract_entities_from_text("My name is John and date is tomorrow")
    assert t1["name"] == "John"
    assert t1["date"] == "tomorrow"

    # Customer updates date to Friday
    t2 = QualificationStageEngine.extract_entities_from_text("Actually please change date to Friday", existing_fields=t1)
    assert t2["date"] in ("Friday", "friday")
    assert t2["name"] == "John"


def test_scenario_6_refuse_optional_field():
    """Customer refuses to give an optional field or gives general feedback."""
    script = (
        "1. Identify Issue\n"
        "2. Product Model\n"
        "3. Purchase Date\n"
        "4. Contact Number"
    )
    stages = QualificationStageEngine.extract_stages_from_script(script)
    
    t1 = QualificationStageEngine.extract_entities_from_text("My phone screen is broken on Samsung Galaxy")
    steps, current_name, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, t1, 0)
    assert steps[0]["status"] == "DONE"  # Issue captured
    assert steps[1]["status"] == "CURRENT" or steps[1]["status"] == "DONE"


def test_scenario_7_script_with_no_qualification_requirements():
    """Script with no explicit qualification steps falls back safely with NO hardcoded industry keywords."""
    empty_script = "Hello, welcome to our company. How can we help you today?"
    stages = QualificationStageEngine.extract_stages_from_script(empty_script, business_name="TechCorp")
    
    for s in stages:
        name_lower = s["name"].lower()
        for forbidden in ["doctor", "hospital", "salon", "stylist", "chennai"]:
            assert forbidden not in name_lower


def test_scenario_8_multi_tenant_isolation():
    """Different agents from different orgs generate completely isolated qualification stages."""
    # Healthcare Agent Script
    script_health = "1. Patient Condition\n2. Care Type Needed\n3. Start Date"
    stages_health = QualificationStageEngine.extract_stages_from_script(script_health, agent_name="DocBot", business_name="MediCare")

    # Automotive Agent Script
    script_auto = "1. Car Model\n2. Service Type\n3. Preferred Drop-off Time"
    stages_auto = QualificationStageEngine.extract_stages_from_script(script_auto, agent_name="AutoBot", business_name="SpeedMotors")

    health_names = [s["name"] for s in stages_health]
    auto_names = [s["name"] for s in stages_auto]

    assert any("Patient" in n or "Condition" in n for n in health_names)
    assert not any("Car Model" in n for n in health_names)
    assert any("Car Model" in n for n in auto_names)
    assert not any("Patient" in n for n in auto_names)


def test_scenario_9_numeric_disambiguation():
    """Accurately disambiguates Age vs Phone vs Budget vs Time vs Date."""
    # Age (1-120)
    e1 = QualificationStageEngine.extract_entities_from_text("She is 23")
    assert e1.get("age") == 23 or e1.get("patient_age") == 23

    # Phone Number (10 digits)
    e2 = QualificationStageEngine.extract_entities_from_text("Her number is 9853107123")
    assert e2.get("phone") == "9853107123" or e2.get("contact_number") == "9853107123"

    # Budget (Currency / Lakhs)
    e3 = QualificationStageEngine.extract_entities_from_text("My budget is 80 lakhs")
    assert "80" in str(e3.get("budget", "")) and "lakh" in str(e3.get("budget", "")).lower()

    # Time (9 AM)
    e4 = QualificationStageEngine.extract_entities_from_text("Tomorrow at 9 AM")
    assert e4.get("date") == "tomorrow"
    assert "9" in str(e4.get("time", ""))


def test_scenario_10_language_switch_detection():
    """Verifies that 'No, continue in Tamil' is detected as a language switch event, not negative_response or echo."""
    switch_inputs = [
        "No, continue in Tamil.",
        "continue in Tamil",
        "please speak in hindi",
        "தமிழ்ல பேசுங்க",
        "தமிழில் தொடரவும்",
        "talk to me in english",
        "telugu please",
    ]
    for text in switch_inputs:
        res = detect_explicit_language_switch_request(text)
        assert res is not None, f"Failed to detect language switch for: {text}"
        lang_code, ack = res
        assert lang_code in ("ta", "hi", "en", "te", "ml", "kn")
        assert len(ack) > 5


def test_scenario_11_real_estate_multi_field_extraction():
    """Customer says: 'My name is Arun, I'm looking for a 2BHK around OMR under 80 lakhs.'"""
    utterance = "My name is Arun, I'm looking for a 2BHK around OMR under 80 lakhs."
    e = QualificationStageEngine.extract_entities_from_text(utterance)
    assert e.get("name") == "Arun" or e.get("customer_name") == "Arun"
    assert "2bhk" in str(e.get("service", "")).lower() or "2bhk" in str(e.get("property_type", "")).lower()
    assert e.get("location") == "Omr" or e.get("location") == "OMR" or e.get("preferred_location") in ("OMR", "Omr")
    assert "80" in str(e.get("budget", ""))


def test_scenario_12_physiotherapy_complete_flow():
    """Complete end-to-end simulation of the Physiotherapy qualification test."""
    script_physio = (
        "1. Identify requested service\n"
        "2. Appointment date and time\n"
        "3. Patient name\n"
        "4. Patient age\n"
        "5. Contact number\n"
        "6. Confirmation"
    )
    stages = QualificationStageEngine.extract_stages_from_script(script_physio, agent_name="CareAgent", business_name="PhysioPlus")
    
    # Step 1: Customer wants physiotherapy
    e1 = QualificationStageEngine.extract_entities_from_text("I want to book a physiotherapy.", script_text=script_physio)
    assert "physiotherapy" in str(e1.get("service", "")).lower()
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e1, 0)
    assert steps[0]["status"] == "DONE"

    # Step 2: Customer gives Tomorrow at 9 AM
    e2 = QualificationStageEngine.extract_entities_from_text("Tomorrow at 9 AM.", existing_fields=e1, script_text=script_physio)
    assert e2.get("date") == "tomorrow"
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e2, 0)
    assert steps[1]["status"] == "DONE"

    # Step 3: Patient name is Jasmine
    e3 = QualificationStageEngine.extract_entities_from_text("Patient name is Jasmine.", existing_fields=e2, script_text=script_physio)
    assert e3.get("name") == "Jasmine" or e3.get("patient_name") == "Jasmine"
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e3, 0)
    assert steps[2]["status"] == "DONE"

    # Step 4: She is 23
    e4 = QualificationStageEngine.extract_entities_from_text("She is 23.", existing_fields=e3, script_text=script_physio)
    assert e4.get("age") == 23 or e4.get("patient_age") == 23
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e4, 0)
    assert steps[3]["status"] == "DONE"

    # Step 5: Her number is 9853107123
    e5 = QualificationStageEngine.extract_entities_from_text("Her number is 9853107123.", existing_fields=e4, script_text=script_physio)
    assert e5.get("phone") == "9853107123" or e5.get("contact_number") == "9853107123"
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e5, 0)
    assert steps[4]["status"] == "DONE"

    # Step 6: Customer switches language
    switch = detect_explicit_language_switch_request("No, continue in Tamil.")
    assert switch is not None
    assert switch[0] == "ta"

    # Verify all previous collected facts are preserved
    assert e5.get("name") == "Jasmine" or e5.get("patient_name") == "Jasmine"
    assert e5.get("date") == "tomorrow"
    assert e5.get("age") == 23
    assert e5.get("phone") == "9853107123"


if __name__ == "__main__":
    test_scenario_1_service_name_date_location()
    print("[PASS] Test 1: Agent A (Service -> Name -> Date -> Location)")
    test_scenario_2_requirement_budget_location_availability()
    print("[PASS] Test 2: Agent B (Requirement -> Budget -> Location -> Availability)")
    test_scenario_3_multi_field_single_utterance()
    print("[PASS] Test 3: Multi-field extraction in single utterance")
    test_scenario_4_out_of_order_answers()
    print("[PASS] Test 4: Out-of-order customer answers")
    test_scenario_5_customer_changes_earlier_answer()
    print("[PASS] Test 5: Customer updates previous answer")
    test_scenario_6_refuse_optional_field()
    print("[PASS] Test 6: Handles natural conversation flow")
    test_scenario_7_script_with_no_qualification_requirements()
    print("[PASS] Test 7: Fallback with zero hardcoded domain keywords")
    test_scenario_8_multi_tenant_isolation()
    print("[PASS] Test 8: Strict multi-tenant and multi-agent script isolation")
    test_scenario_9_numeric_disambiguation()
    print("[PASS] Test 9: Numeric disambiguation (Age vs Phone vs Budget vs Time)")
    test_scenario_10_language_switch_detection()
    print("[PASS] Test 10: Explicit language switch detection ('No, continue in Tamil')")
    test_scenario_11_real_estate_multi_field_extraction()
    print("[PASS] Test 11: Real estate multi-field extraction (Name, 2BHK, OMR, 80 lakhs)")
    test_scenario_12_physiotherapy_complete_flow()
    print("[PASS] Test 12: Complete physiotherapy test flow with state preservation")
    print("\nALL 12 DYNAMIC QUALIFICATION & ISOLATION TEST CASES PASSED SUCCESSFULLY!")
