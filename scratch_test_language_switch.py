"""
Verification Script for CallZenza Language Switch + Conversation Continuity + Duplicate Prevention.
Tests user requirements from Section 29 and Section 30.
"""
import asyncio
import sys
import os

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Add backend directory to path
sys.path.insert(0, r"d:\Aminoid\callzenza cloned\callzenza-backend")

from app.services.language_service import (
    detect_explicit_language_switch_request,
    detect_speech_language,
)
from app.services.ai.qualification_service import QualificationStageEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.ai.prompt_service import PromptService


def test_language_switch_detection():
    print("\n--- TEST 1: Language Switch Detection ---")
    test_phrases_valid = [
        "Can you talk in Tamil?",
        "You can talk in Tamil.",
        "Please speak Tamil.",
        "Tamil Pes",
        "Tamil Tamil Tamil",
        "தமிழ்ல பேசுங்க",
        "எனக்கு தமிழ்ல பேச",
        "தமிழில் பேசுங்கள்",
        "हिंदी में बात करें",
        "Can you speak Hindi?",
        "English please",
    ]
    for p in test_phrases_valid:
        res = detect_explicit_language_switch_request(p)
        print(f"Phrase: '{p}' -> Detected: {res}")
        assert res is not None, f"Failed to detect valid language switch in '{p}'"

    test_phrases_invalid = [
        "appointment",
        "home",
        "okay",
        "yes",
        "coming",
        "proceed",
        "சொந்த வீடு",
        "தனி வீடு",
        "631001",
    ]
    for p in test_phrases_invalid:
        res = detect_explicit_language_switch_request(p)
        print(f"Non-switch phrase: '{p}' -> Detected: {res}")
        assert res is None, f"False positive language switch detected for '{p}'"
    print(">>> TEST 1 PASSED: Language switch detection is 100% accurate.")


def test_speech_language_persistence():
    print("\n--- TEST 2: Speech Language Loan Word Persistence ---")
    # In an ongoing Tamil conversation, single English loan words must NOT flip language back to English
    loan_words = ["appointment", "home", "okay", "yes", "coming", "proceed", "631001", "single family", "owner"]
    for word in loan_words:
        detected = detect_speech_language(user_text=word, current_session_lang="ta", script_lang="en")
        print(f"Loan word: '{word}' in session_lang='ta' -> Result: '{detected}'")
        assert detected == "ta", f"Loan word '{word}' flipped language away from 'ta' to '{detected}'"
    print(">>> TEST 2 PASSED: Loan words do not flip conversation language.")


def test_multi_field_extraction():
    print("\n--- TEST 3: Multi-Field Extraction & Aliases ---")
    
    # 1. Single utterance with multiple fields
    u1 = "சொந்த வீடு. தனி வீடு. வரியும் நான்தான் செலுத்துறேன்."
    extracted = QualificationStageEngine.extract_entities_from_text(u1)
    print(f"Utterance: '{u1}'\nExtracted: {extracted}")
    assert extracted.get("home_ownership") == "owner", "home_ownership failed"
    assert extracted.get("property_type") == "single_family", "property_type failed"
    assert extracted.get("tax_payer") is True, "tax_payer failed"

    # 2. Name extraction
    u2 = "என் பெயர் முகிலன் விஸ்வநாதன்."
    ext2 = QualificationStageEngine.extract_entities_from_text(u2, existing_fields=extracted)
    print(f"Utterance: '{u2}'\nExtracted: {ext2}")
    assert "Mugilan" in ext2.get("name", "") or "முகிலன்" in ext2.get("name", ""), "Name extraction failed"

    # 3. Zip code extraction
    u3 = "631001"
    ext3 = QualificationStageEngine.extract_entities_from_text(u3, existing_fields=ext2)
    print(f"Utterance: '{u3}'\nExtracted: {ext3}")
    assert ext3.get("zip_code") == "631001" or ext3.get("pincode") == "631001", "Zip code extraction failed"

    # 4. Service / Appointment intent
    u4 = "எனக்கு appointment book பண்ணனும்."
    ext4 = QualificationStageEngine.extract_entities_from_text(u4)
    print(f"Utterance: '{u4}'\nExtracted: {ext4}")
    assert ext4.get("service_required") == "appointment", "Service required extraction failed"

    print(">>> TEST 3 PASSED: Multi-field entity extraction and Tamil patterns work correctly.")


def test_section_29_dialogue_flow():
    print("\n--- TEST 4: Section 29 Exact Conversation Flow Simulation ---")
    agent_id = "test_agent_solar"
    session_id = "sess_sec_29_test"
    ConversationMemoryService.clear_history(agent_id, session_id)
    ConversationMemoryService.set_call_direction(agent_id, session_id, "outbound")
    ConversationMemoryService.set_language(agent_id, session_id, "en")

    script_text = """
    1. Greeting: Introduce Magic from Home Solar Experts and check home ownership.
    2. Home Ownership: Ask if the customer rents or owns their home.
    3. Property Type: Ask if it is a single-family home or apartment.
    4. Tax Payer: Ask if they pay property or income taxes.
    5. Contact Details: Collect customer name and zip code.
    6. Confirmation: Confirm solar appointment booking.
    """

    stages = QualificationStageEngine.extract_stages_from_script(script_text, agent_name="MAGIC", business_name="Home Solar Experts")
    print(f"Extracted stages from script ({len(stages)} stages):")
    for s in stages:
        print(f" - {s['name']} (field: {s.get('field')})")

    # Turn 0: Outbound Greeting
    current_step_idx = 0
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields={}, current_step_idx=0
    )
    print(f"\n[Turn 0 - Initial Outbound] Stage: {stage_name}, Next Field: {next_field}, Missing: {missing}")

    # Turn 1: Customer: "Can you talk in Tamil?"
    customer_turn_1 = "Can you talk in Tamil?"
    switch_res = detect_explicit_language_switch_request(customer_turn_1)
    assert switch_res is not None
    target_lang = switch_res[0]
    ConversationMemoryService.set_language(agent_id, session_id, target_lang)
    assert ConversationMemoryService.get_language(agent_id, session_id) == "ta"

    # Evaluate progress after language switch (NO STATE LOST)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 1 - After Lang Switch to Tamil] Stage: {stage_name}, Next Field: {next_field}, Language: {target_lang}")
    assert stage_name != "1. Greeting", "Stage should not restart"
    assert target_lang == "ta"

    # Build prompt and verify instructions
    prompt = PromptService.build_system_prompt(
        agent_name="MAGIC",
        business_name="Home Solar Experts",
        detected_language="ta",
        detected_intent="language_switch",
        call_direction="outbound",
        script_full_text=script_text,
        qualification_state=known,
        missing_fields=missing,
        current_stage=stage_name,
        next_field=next_field,
    )
    # Check that prompt forbids inbound restart
    assert "DO NOT ask 'How can I help you?'" in prompt or "எப்படி உதவலாம்" in prompt
    assert "Acknowledge the language switch with a very brief phrase" in prompt

    # Turn 2: Customer: "எனக்கு appointment book பண்ணனும்."
    customer_turn_2 = "எனக்கு appointment book பண்ணனும்."
    extracted_2 = QualificationStageEngine.extract_entities_from_text(customer_turn_2, existing_fields=known)
    for k, v in extracted_2.items():
        ConversationMemoryService.set_entity(agent_id, session_id, k, v)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 2 - Appointment Request] Stage: {stage_name}, Known: {known}, Next: {next_field}")

    # Turn 3: Customer: "சொந்த வீடு."
    customer_turn_3 = "சொந்த வீடு."
    extracted_3 = QualificationStageEngine.extract_entities_from_text(customer_turn_3, existing_fields=known)
    for k, v in extracted_3.items():
        ConversationMemoryService.set_entity(agent_id, session_id, k, v)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 3 - Home Ownership Answered] Stage: {stage_name}, Known: {known}, Next: {next_field}")
    assert known.get("home_ownership") == "owner"

    # Turn 4: Customer provides multiple answers: "தனி வீடு. வரியும் நான்தான் செலுத்துறேன்."
    customer_turn_4 = "தனி வீடு. வரியும் நான்தான் செலுத்துறேன்."
    extracted_4 = QualificationStageEngine.extract_entities_from_text(customer_turn_4, existing_fields=known)
    for k, v in extracted_4.items():
        ConversationMemoryService.set_entity(agent_id, session_id, k, v)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 4 - Multi-field Answered] Stage: {stage_name}, Known: {known}, Next: {next_field}")
    assert known.get("property_type") == "single_family"
    assert known.get("tax_payer") is True

    # Turn 5: Customer says "Tamil Pes" again mid-call
    customer_turn_5 = "Tamil Pes"
    switch_res_5 = detect_explicit_language_switch_request(customer_turn_5)
    assert switch_res_5 is not None
    # Verify state is completely preserved!
    known_after_5 = ConversationMemoryService.get_entities(agent_id, session_id)
    assert known_after_5.get("home_ownership") == "owner"
    assert known_after_5.get("property_type") == "single_family"
    assert known_after_5.get("tax_payer") is True
    steps, stage_name_5, active_idx, qual_status, missing, next_field_5 = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known_after_5, current_step_idx=active_idx
    )
    print(f"[Turn 5 - Second Lang Switch 'Tamil Pes'] Stage: {stage_name_5}, Next: {next_field_5}")
    # Ownership, property type, tax payer must NOT be in missing or next_field
    assert "Home Ownership" not in missing
    assert "Property Type" not in missing
    assert "Tax Payer" not in missing

    # Turn 6: Customer: "என் பெயர் முகிலன் விஸ்வநாதன்."
    customer_turn_6 = "என் பெயர் முகிலன் விஸ்வநாதன்."
    extracted_6 = QualificationStageEngine.extract_entities_from_text(customer_turn_6, existing_fields=known_after_5)
    for k, v in extracted_6.items():
        ConversationMemoryService.set_entity(agent_id, session_id, k, v)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 6 - Name Answered] Stage: {stage_name}, Known: {known}, Next: {next_field}")

    # Turn 7: Customer: "631001"
    customer_turn_7 = "631001"
    extracted_7 = QualificationStageEngine.extract_entities_from_text(customer_turn_7, existing_fields=known)
    for k, v in extracted_7.items():
        ConversationMemoryService.set_entity(agent_id, session_id, k, v)
    known = ConversationMemoryService.get_entities(agent_id, session_id)
    steps, stage_name, active_idx, qual_status, missing, next_field = QualificationStageEngine.evaluate_progress(
        stages=stages, collected_fields=known, current_step_idx=active_idx
    )
    print(f"[Turn 7 - Zip Answered] Stage: {stage_name}, Status: {qual_status}, Known: {known}")
    assert known.get("zip_code") == "631001" or known.get("pincode") == "631001"
    assert qual_status == "QUALIFIED"

    print(">>> TEST 4 PASSED: Exact Section 29 test flow succeeded without resetting state or repeating questions.")


def test_section_30_inbound_vs_outbound():
    print("\n--- TEST 5: Section 30 Inbound vs Outbound Dynamic Behavior ---")
    # Test Inbound prompt configuration
    p_inbound = PromptService.build_system_prompt(
        agent_name="MAGIC",
        business_name="CallZenza Support",
        detected_language="ta",
        detected_intent="language_switch",
        call_direction="inbound",
        current_stage="Service Inquiry",
    )
    assert "INBOUND CALL BEHAVIOR" in p_inbound
    print("Inbound prompt verified.")

    # Test Outbound prompt configuration
    p_outbound = PromptService.build_system_prompt(
        agent_name="MAGIC",
        business_name="Home Solar Experts",
        detected_language="ta",
        detected_intent="language_switch",
        call_direction="outbound",
        current_stage="Home Ownership",
    )
    assert "OUTBOUND CALL BEHAVIOR" in p_outbound
    assert "DO NOT ask 'How can I help you?'" in p_outbound or "எப்படி உதவலாம்" in p_outbound
    print("Outbound prompt verified.")
    print(">>> TEST 5 PASSED: Inbound vs Outbound dynamically control conversation behavior.")


if __name__ == "__main__":
    test_language_switch_detection()
    test_speech_language_persistence()
    test_multi_field_extraction()
    test_section_29_dialogue_flow()
    test_section_30_inbound_vs_outbound()
    print("\n=======================================================")
    print("ALL TESTS PASSED WITH 100% SUCCESS!")
    print("=======================================================")
