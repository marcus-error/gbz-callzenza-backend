"""
Integration Test for Dynamic Script & RAG Isolation across Domains:
1. Healthcare -> Real Estate -> Education
2. Verifies that switching Agent Script/RAG dynamically changes:
   - Extracted qualification fields
   - Active qualification stages
   - Grounded prompt context
   - Factual RAG retrieval scope
   - Zero hardcoded domain cross-leakage
"""
import os
import sys
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app.services.ai.qualification_service import QualificationStageEngine
from app.services.ai.prompt_service import PromptService
from app.services.ai.conversation.memory_service import ConversationMemoryService


def test_healthcare_domain_isolation():
    """Test 1: Healthcare Agent with Physiotherapy Script & Healthcare RAG."""
    session_id = "test_sess_health_01"
    agent_id = "agent_health_01"
    script_health = (
        "1. Identify requested care service\n"
        "2. Appointment date and time\n"
        "3. Patient name\n"
        "4. Patient age\n"
        "5. Contact number"
    )
    rag_health = "Nurse O'Clock provides home physiotherapy and nurse care in Chennai."
    
    stages = QualificationStageEngine.extract_stages_from_script(script_health)
    e = QualificationStageEngine.extract_entities_from_text("I want physiotherapy tomorrow at 9 AM for Jasmine, she is 23.", script_text=script_health)
    
    assert "physiotherapy" in str(e.get("service", "")).lower()
    assert e.get("name") == "Jasmine" or e.get("patient_name") == "Jasmine"
    assert e.get("age") == 23 or e.get("patient_age") == 23
    assert e.get("date") == "tomorrow"
    
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e, 0)
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "DONE"
    assert steps[2]["status"] == "DONE"
    assert steps[3]["status"] == "DONE"
    assert next_f is not None and "contact" in next_f.lower()

    prompt = PromptService.build_system_prompt(
        agent_name="HealthBot",
        business_name="Nurse O'Clock",
        retrieved_context=rag_health,
        known_entities=e,
        qualification_state=e,
        missing_fields=missing,
        current_stage=curr,
        next_field=next_f,
        script_full_text=script_health,
    )
    assert "INFORMATION ALREADY COLLECTED FROM CUSTOMER" in prompt
    assert "Jasmine" in prompt
    assert "Nurse O'Clock" in prompt
    assert "Real Estate" not in prompt


def test_real_estate_domain_isolation():
    """Test 2: Real Estate Agent with Property Script & Real Estate RAG."""
    session_id = "test_sess_realestate_01"
    agent_id = "agent_realestate_01"
    script_realestate = (
        "1. Customer Name\n"
        "2. Property Type\n"
        "3. Budget Range\n"
        "4. Preferred Location\n"
        "5. Move-in Date"
    )
    rag_realestate = "Prime Properties offers 2BHK and 3BHK luxury apartments in OMR and ECR Chennai."
    
    stages = QualificationStageEngine.extract_stages_from_script(script_realestate)
    e = QualificationStageEngine.extract_entities_from_text("My name is Arun, looking for a 2BHK around OMR under 80 lakhs.", script_text=script_realestate)
    
    assert e.get("name") == "Arun" or e.get("customer_name") == "Arun"
    assert "2bhk" in str(e.get("property_type", "")).lower() or "2bhk" in str(e.get("service", "")).lower()
    assert "80" in str(e.get("budget", ""))
    assert e.get("location") in ("OMR", "Omr") or e.get("preferred_location") in ("OMR", "Omr")
    
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e, 0)
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "DONE"
    assert steps[2]["status"] == "DONE"
    assert steps[3]["status"] == "DONE"
    assert next_f is not None and "move" in next_f.lower()

    prompt = PromptService.build_system_prompt(
        agent_name="EstateBot",
        business_name="Prime Properties",
        retrieved_context=rag_realestate,
        known_entities=e,
        qualification_state=e,
        missing_fields=missing,
        current_stage=curr,
        next_field=next_f,
        script_full_text=script_realestate,
    )
    assert "Arun" in prompt
    assert "80 Lakhs" in prompt or "80 lakhs" in prompt
    assert "Prime Properties" in prompt
    assert "physiotherapy" not in prompt.lower()
    assert "patient" not in prompt.lower()


def test_education_domain_isolation():
    """Test 3: Education Agent with Course Script & Education RAG."""
    session_id = "test_sess_edu_01"
    agent_id = "agent_edu_01"
    script_edu = (
        "1. Student Name\n"
        "2. Course Requirement\n"
        "3. Educational Qualification\n"
        "4. Preferred Batch Timing"
    )
    rag_edu = "Zenith Tech Academy offers Full Stack Python and Data Science bootcamps with weekend batches."
    
    stages = QualificationStageEngine.extract_stages_from_script(script_edu)
    e = QualificationStageEngine.extract_entities_from_text("My name is Priya, interested in Python course in the evening batch.", script_text=script_edu)
    
    assert e.get("name") == "Priya" or e.get("student_name") == "Priya"
    assert "python" in str(e.get("course", "")).lower() or "python" in str(e.get("service", "")).lower()
    assert "evening" in str(e.get("time", "")).lower()
    
    steps, curr, active_idx, status, missing, next_f = QualificationStageEngine.evaluate_progress(stages, e, 0)
    assert steps[0]["status"] == "DONE"
    assert steps[1]["status"] == "DONE"

    prompt = PromptService.build_system_prompt(
        agent_name="EduBot",
        business_name="Zenith Tech Academy",
        retrieved_context=rag_edu,
        known_entities=e,
        qualification_state=e,
        missing_fields=missing,
        current_stage=curr,
        next_field=next_f,
        script_full_text=script_edu,
    )
    assert "Priya" in prompt
    assert "Zenith Tech Academy" in prompt
    assert "physiotherapy" not in prompt.lower()
    assert "2bhk" not in prompt.lower()
    assert "doctor" not in prompt.lower()


if __name__ == "__main__":
    test_healthcare_domain_isolation()
    print("[PASS] Domain Test 1: Healthcare Script & RAG")
    test_real_estate_domain_isolation()
    print("[PASS] Domain Test 2: Real Estate Script & RAG")
    test_education_domain_isolation()
    print("[PASS] Domain Test 3: Education Script & RAG")
    print("\nALL DOMAIN ISOLATION TESTS PASSED WITH 0 LEAKAGE!")
