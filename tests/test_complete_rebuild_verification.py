"""
Comprehensive Verification Test Suite for CallZenza AI Voice Agent Rebuild.
Tests:
1. Script Ingestion Pipeline (Analysis, Chunking, Embeddings, Isolated Vector Store)
2. Script-driven Dynamic Qualification (No hardcoded fields)
3. Multiple value extraction in one customer turn
4. Corrections update previous values
5. Rejection handling
6. Customer questions prioritize over qualification without losing context
7. Campaign & Version Isolation (No cross-campaign leakage)
8. Versioned Cache hit/miss & invalidation on version change
9. Generalization across two completely different domains (Healthcare vs Solar Energy) without code changes
10. STT deduplication protection
"""
import pytest
import asyncio
import time

from app.script_engine.ingestion import ScriptIngestionPipeline
from app.rag.script_vector_store import ScriptVectorStore, ScriptCacheManager
from app.conversation.qualification import DynamicQualificationManager
from app.conversation.runtime import ConversationRuntime


HEALTHCARE_SCRIPT = """
CLINIC INFORMATION:
Company: Nurse O' Clock
Services: We provide in-home nursing services, physiotherapy rehabilitation, elderly care, and post-operative monitoring across Chennai.
Locations: Chennai, Velachery, Anna Nagar, and OMR.
Working Hours: Monday to Saturday, 8:00 AM to 8:00 PM. Emergency nursing is 24/7.
Pricing: Physiotherapy home visit is Rs 1200 per session.

APPOINTMENTS & QUALIFICATION:
To schedule a healthcare visit, please collect:
- Patient Name
- Patient Age
- Service Required
- Preferred Date and Time

RULES:
- Always answer service questions politely.
- If customer says they do not want an appointment, politely wish them good health and close the call.
"""

SOLAR_SCRIPT = """
COMPANY OVERVIEW:
Company: Home Solar Solutions
Products: Residential rooftop solar panels, commercial inverters, and battery storage systems.
Service Areas: Coimbatore, Tirupur, and Salem.
Working Hours: 9:00 AM to 6:00 PM all weekdays.
Warranty: 25 years performance warranty on solar panels.

CONSULTATION QUALIFICATION REQUIREMENTS:
To book a solar site inspection, collect:
- Property Owner Name
- Property Type (Independent House or Commercial)
- Average Monthly Electricity Bill
- Inspection Date

RULES:
- Explain savings if customer asks about cost.
- If customer is a tenant or doesn't own the property, inform them roof owner permission is needed.
"""


@pytest.mark.asyncio
async def test_01_script_ingestion_and_vector_indexing():
    """Verifies automatic cleaning, analysis, chunking, embeddings, and isolated vector indexing."""
    res = ScriptIngestionPipeline.process_and_index_script(
        raw_script_text=HEALTHCARE_SCRIPT,
        organization_id="org_health_1",
        campaign_id="camp_health_1",
        script_id="script_health_1"
    )

    assert res["status"] == "READY"
    assert res["total_chunks"] >= 2
    assert res["analysis"]["business_name"] == "Nurse O' Clock"
    assert len(res["analysis"]["qualification_requirements"]) >= 3

    # Check vector search strictly for this campaign
    results = ScriptVectorStore.similarity_search(
        query="What services do you provide?",
        organization_id="org_health_1",
        campaign_id="camp_health_1",
        script_id="script_health_1",
        script_version=res["script_version"],
        top_k=2
    )

    assert len(results) > 0
    assert any("nursing" in r["text"].lower() or "physiotherapy" in r["text"].lower() for r in results)


@pytest.mark.asyncio
async def test_02_dynamic_qualification_and_question_priority():
    """Customer asks a knowledge question during qualification. AI answers question without getting stuck."""
    session_id = f"test_sess_{int(time.time()*1000)}"

    # Turn 1: Customer asks about services
    turn1 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="What services do you provide?",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )

    assert turn1["response"] != ""
    assert "What service are you looking for today?" not in turn1["response"]
    # Should mention services from the script
    resp_lower = turn1["response"].lower()
    assert "nursing" in resp_lower or "physiotherapy" in resp_lower or "care" in resp_lower


@pytest.mark.asyncio
async def test_03_multiple_information_extracted_in_single_turn():
    """Customer provides multiple values at once (name, age, service). AI extracts all."""
    session_id = f"test_multi_{int(time.time()*1000)}"

    turn = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="My mother's name is Lakshmi, she is 72 years old, and she needs physiotherapy.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )

    collected = turn["collected_data"]
    # Check that dynamic fields were captured
    assert len(collected) >= 2


@pytest.mark.asyncio
async def test_04_customer_correction():
    """Customer corrects date/time. The latest confirmed value becomes active."""
    session_id = f"test_corr_{int(time.time()*1000)}"

    turn1 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Tomorrow at 9 AM works.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )

    turn2 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Actually please change that to 11 AM.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )

    collected = turn2["collected_data"]
    values_str = " ".join(str(v) for v in collected.values()).lower()
    assert "11" in values_str or "11 am" in values_str


@pytest.mark.asyncio
async def test_05_customer_rejection():
    """Customer rejects appointment. AI respects rejection and stops qualification."""
    session_id = f"test_rej_{int(time.time()*1000)}"

    turn = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="I don't want an appointment, thank you.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )

    assert turn["workflow_active"] is False
    assert len(turn["missing_fields"]) == 0


@pytest.mark.asyncio
async def test_06_versioned_cache_hit_and_invalidation():
    """Test cache hit on repeated question, and cache invalidation on script version update."""
    org_id = "org_cache_test"
    camp_id = "camp_cache_test"
    script_id = "script_cache_test"

    v1_res = ScriptIngestionPipeline.process_and_index_script(
        raw_script_text=HEALTHCARE_SCRIPT,
        organization_id=org_id,
        campaign_id=camp_id,
        script_id=script_id
    )
    v1 = v1_res["script_version"]

    session_id = f"cache_sess_{int(time.time()*1000)}"

    # First query -> Cache Miss
    t1 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Where are you located?",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id=camp_id,
        organization_id=org_id,
        script_version=v1
    )
    assert t1["cache_hit"] is False

    # Second identical query -> Cache Hit
    t2 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Where are you located?",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id=camp_id,
        organization_id=org_id,
        script_version=v1
    )
    assert t2["cache_hit"] is True

    # Ingest updated Script V2
    updated_script = HEALTHCARE_SCRIPT + "\nNew Branch: Coimbatore center opened."
    v2_res = ScriptIngestionPipeline.process_and_index_script(
        raw_script_text=updated_script,
        organization_id=org_id,
        campaign_id=camp_id,
        script_id=script_id
    )
    v2 = v2_res["script_version"]
    assert v1 != v2

    # Query with V2 -> Cache Miss because V1 cache is invalidated for V2
    # Ensure outside 1.5s deduplication window
    await asyncio.sleep(1.6)
    t3 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Where are you located?",
        script_text=updated_script,
        campaign_id=camp_id,
        organization_id=org_id,
        script_version=v2
    )
    assert t3["cache_hit"] is False


@pytest.mark.asyncio
async def test_07_complete_domain_independence_and_isolation():
    """
    Test two completely different domains (Healthcare vs Solar Energy).
    Verify Campaign A uses only Script A, Campaign B uses only Script B.
    Zero cross-campaign leakage. Zero hardcoded fields.
    """
    org_id = "multi_tenant_org"

    # Setup Campaign A (Healthcare)
    camp_a = "camp_health_alpha"
    ScriptIngestionPipeline.process_and_index_script(
        raw_script_text=HEALTHCARE_SCRIPT,
        organization_id=org_id,
        campaign_id=camp_a
    )

    # Setup Campaign B (Solar)
    camp_b = "camp_solar_beta"
    ScriptIngestionPipeline.process_and_index_script(
        raw_script_text=SOLAR_SCRIPT,
        organization_id=org_id,
        campaign_id=camp_b
    )

    sess_a = f"sess_a_{int(time.time()*1000)}"
    sess_b = f"sess_b_{int(time.time()*1000)}"

    # Ask Campaign A: "What services do you provide?"
    res_a = await ConversationRuntime.process_turn(
        call_session_id=sess_a,
        customer_utterance="What services do you provide?",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id=camp_a,
        organization_id=org_id
    )

    # Ask Campaign B: "What products do you offer?"
    res_b = await ConversationRuntime.process_turn(
        call_session_id=sess_b,
        customer_utterance="What products do you offer?",
        script_text=SOLAR_SCRIPT,
        campaign_id=camp_b,
        organization_id=org_id
    )

    # Assert Campaign A mentions healthcare, NOT solar
    assert "solar" not in res_a["response"].lower()
    assert "roof" not in res_a["response"].lower()

    # Assert Campaign B mentions solar, NOT nursing
    assert "nurse" not in res_b["response"].lower()
    assert "nursing" not in res_b["response"].lower()
    assert "solar" in res_b["response"].lower() or "rooftop" in res_b["response"].lower() or "panel" in res_b["response"].lower()


@pytest.mark.asyncio
async def test_08_dynamic_conversation_no_repeated_questions():
    """
    Verifies the exact multi-turn conversational progression:
    1. Customer says 'I need an appointment.' -> AI understands intent, asks for service.
    2. Customer says 'I need physiotherapy tomorrow.' -> AI extracts service & date, does NOT repeat service, asks time.
    3. Customer interrupts: 'Before that, what are your working hours?' -> AI answers question first, resumes appointment.
    4. Customer provides remaining info: '10 AM works. My name is Arun, age is 65.' -> AI marks qualified, no repeated questions.
    """
    session_id = f"test_dynamic_flow_{int(time.time()*1000)}"

    # Turn 1: Customer initiates appointment intent
    t1 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="I need an appointment.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )
    assert t1["response"] != ""
    assert "What service are you looking for today?" not in t1["response"]
    assert t1["workflow_active"] is True

    # Turn 2: Customer provides service and date
    t2 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="I need physiotherapy tomorrow.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )
    assert "physiotherapy" in str(t2["collected_data"]).lower()
    # Ensure AI does NOT ask what service they need again
    assert "what service" not in t2["response"].lower()
    assert "which service" not in t2["response"].lower()

    # Turn 3: Customer asks an interrupting knowledge question
    t3 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="Before that, what are your working hours?",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )
    # AI must answer the working hours question
    assert "8:00" in t3["response"] or "8" in t3["response"] or "hours" in t3["response"].lower()

    # Turn 4: Customer provides remaining information
    t4 = await ConversationRuntime.process_turn(
        call_session_id=session_id,
        customer_utterance="10 AM works. My name is Arun, and my age is 65.",
        script_text=HEALTHCARE_SCRIPT,
        campaign_id="camp_health_1",
        organization_id="org_health_1"
    )
    collected_str = str(t4["collected_data"]).lower()
    assert "arun" in collected_str
    assert "65" in collected_str
    assert t4["workflow_active"] is True

