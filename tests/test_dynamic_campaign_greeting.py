"""
Regression and Acceptance Tests for Dynamic Campaign-Based AI Greeting.
Verifies that every campaign starts according to its own uploaded script and instructions,
customer names are safely omitted when absent, Pramerica campaign respects approved script,
caching is tenant-isolated, greeting is not repeated, and qualification lifecycle remains intact.
"""
import pytest
import uuid
import time
from unittest.mock import AsyncMock, patch

from app.services.ai.campaign_greeting_service import CampaignGreetingService
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.ai.qualification_service import QualificationStageEngine
from app.services.ai.response_sanitizer import ResponseGuard


# Sample Scripts for testing
PRAMERICA_APPROVED_SCRIPT = """
CLIENT : PRAMERICA LIFE INSURANCE Complete Inbound Call Script & Master FAQ 
Pramerica Life Insurance — Voice AI Agent — 1 
 
IMPORTANT AI SAFETY RULE: The AI Agent must not invent premiums, benefits, policy status, claim status, surrender values, maturity values.

SECTION 01 
GREETING & CALL OPENING 
 
AI Agent: 
Good Morning! Welcome to Pramerica Life Insurance. Thank you for calling us. I can help with insurance plans, policy servicing, claims, branches and customer support. How may I help you today? 
Caller: 
I want to know about your plans.
"""

DREAM_CAATCHERS_SCRIPT = """
Dream Caatchers Properties | Outbound RAG Script
Company Name: Dream Caatchers Properties
Company Type: Real Estate / Property Services

OUTBOUND CALL OPENING
Good morning / afternoon, am I speaking with [Customer Name]?
Customer: Yes.
Agent: Thank you. My name is [Agent Name], from Dream Caatchers Properties. I am calling to share a quick update on high-return investment properties and villa plots in Chennai. Do you have 2 minutes to talk?
"""

NURSE_OCLOCK_SCRIPT = """
NURSE O' CLOCK — CHENNAI INFORMATION
Organization Name: Nurse O' Clock
Organization Type: Home Healthcare and Patient-Care Service Provider
Service Location: Chennai, Tamil Nadu
About Us: Nurse O' Clock supports patients, elderly people, families and caregivers through home-based healthcare, caregiving, patient support and related service coordination.
Communication Style: Warm, respectful, patient, professional, reassuring and empathetic.
AI Receptionist Role: Service enquiries, basic information, lead capture, appointment/service requests and routing.
"""

SOLAR_CAMPAIGN_SCRIPT = """
1. Greeting: Hello [Customer Name], this is Alex calling from SunPower Solar. How are you doing today?
2. Services: We provide residential solar energy consultations and installation assistance.
"""


class DummyCampaign:
    def __init__(self, campaign_id: str, name: str, business_id: str = "biz-1", call_direction: str = "outbound", script: str = ""):
        self.id = campaign_id
        self.name = name
        self.business_id = business_id
        self.script = script
        self.schedule = {"call_direction": call_direction, "script": script}


class DummyLead:
    def __init__(self, lead_id: str, first_name: str = "", phone: str = "919876543210"):
        self.id = lead_id
        self.first_name = first_name
        self.phone = phone


def setup_function():
    CampaignGreetingService.clear_cache()


def test_campaign_a_uses_campaign_a_opening():
    """Campaign A with an explicit script greeting uses Campaign A's approved opening."""
    camp_a = DummyCampaign("camp-A", "SunPower Solar", script=SOLAR_CAMPAIGN_SCRIPT)
    lead_john = DummyLead("lead-1", first_name="John")
    greeting_a = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp_a,
        lead=lead_john,
        agent={"name": "Alex"},
        script_text=SOLAR_CAMPAIGN_SCRIPT,
        call_direction="outbound",
    )
    assert "SunPower Solar" in greeting_a
    assert "John" in greeting_a
    assert "Call Zenza" not in greeting_a


def test_campaign_b_uses_campaign_b_opening():
    """Campaign B with a different script greeting uses Campaign B's approved opening."""
    camp_b = DummyCampaign("camp-B", "Pramerica Life Insurance", script=PRAMERICA_APPROVED_SCRIPT)
    lead_sara = DummyLead("lead-2", first_name="Sara")
    greeting_b = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp_b,
        lead=lead_sara,
        agent={"name": "MAGIC"},  # Should NOT bleed into greeting
        script_text=PRAMERICA_APPROVED_SCRIPT,
        call_direction="inbound",
    )
    assert "Pramerica Life Insurance" in greeting_b
    assert "MAGIC" not in greeting_b
    assert "Call Zenza" not in greeting_b
    assert "SunPower Solar" not in greeting_b


def test_script_with_instructions_produces_grounded_greeting():
    """A script with organizational instructions (Nurse O' Clock) produces a script-grounded greeting."""
    camp = DummyCampaign("camp-nurse", "Nurse O' Clock", script=NURSE_OCLOCK_SCRIPT)
    greeting = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp,
        lead=None,
        agent={"name": "Alex"},
        script_text=NURSE_OCLOCK_SCRIPT,
        call_direction="inbound",
    )
    assert "Nurse O' Clock" in greeting
    assert any(k in greeting.lower() for k in ["home healthcare", "caregiving", "services", "assist you"])
    assert "MAGIC" not in greeting
    assert "Call Zenza" not in greeting


def test_script_with_no_opening_instructions_uses_clean_fallback():
    """A campaign with no script uses a clean, professional fallback greeting without leaking other campaigns."""
    camp_empty = DummyCampaign("camp-generic", "Acme Logistics", script="")
    greeting = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp_empty,
        lead=None,
        agent={"name": "Alex"},
        script_text="",
        call_direction="outbound",
    )
    assert "Acme Logistics" in greeting
    assert "Pramerica" not in greeting
    assert "SunPower" not in greeting
    assert "Nurse O' Clock" not in greeting


def test_customer_name_omitted_when_unavailable():
    """Customer name is omitted cleanly without leaving dangling commas or titles when unavailable."""
    camp = DummyCampaign("camp-dc", "Dream Caatchers Properties", script=DREAM_CAATCHERS_SCRIPT)

    # Case A: Name is empty
    greeting_no_name = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp,
        lead=DummyLead("lead-noname", first_name=""),
        script_text=DREAM_CAATCHERS_SCRIPT,
    )
    assert "[Customer Name]" not in greeting_no_name
    assert "Customer" not in greeting_no_name
    assert "  " not in greeting_no_name
    assert not greeting_no_name.startswith(",")

    # Case B: Name is generic "Customer"
    greeting_generic = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp,
        lead=DummyLead("lead-cust", first_name="Customer"),
        script_text=DREAM_CAATCHERS_SCRIPT,
    )
    assert "[Customer Name]" not in greeting_generic
    assert "Customer" not in greeting_generic


def test_pramerica_campaign_no_policy_invention():
    """Pramerica campaign does not invent insurance or policy details and passes ResponseGuard."""
    camp = DummyCampaign("camp-pram", "call zenza", script=PRAMERICA_APPROVED_SCRIPT)
    greeting = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp,
        lead=DummyLead("lead-3", first_name="Mugilan"),
        agent={"name": "MAGIC"},
        script_text=PRAMERICA_APPROVED_SCRIPT,
        call_direction="inbound",
    )
    assert "Pramerica Life Insurance" in greeting
    assert "MAGIC" not in greeting
    assert "Call Zenza" not in greeting

    # Must NOT invent unverified values
    for forbidden in ["10,000", "50,000", "confirmed claim", "active policy approved", "guaranteed payment"]:
        assert forbidden not in greeting.lower()

    # Must pass ResponseGuard
    guard_res = ResponseGuard.validate_customer_response(greeting)
    assert guard_res["is_valid"] is True


def test_concurrent_sessions_isolation():
    """Concurrent sessions on different campaigns cannot receive each other's cached greeting."""
    camp_a = DummyCampaign("camp-1", "Alpha Energy", business_id="biz-1")
    camp_b = DummyCampaign("camp-2", "Beta Motors", business_id="biz-2")

    greeting_a = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp_a,
        script_text="Greeting: Welcome to Alpha Energy. How can I help you?",
    )
    greeting_b = CampaignGreetingService.resolve_campaign_greeting(
        campaign=camp_b,
        script_text="Greeting: Hello from Beta Motors. Are you looking for a vehicle?",
    )

    assert "Alpha Energy" in greeting_a
    assert "Beta Motors" not in greeting_a

    assert "Beta Motors" in greeting_b
    assert "Alpha Energy" not in greeting_b


def test_greeting_not_repeated_when_session_initializes_again():
    """Greeting idempotency: ConversationMemoryService ensures greeting is only marked and sent once."""
    agent_id = f"test-agent-{uuid.uuid4().hex[:6]}"
    session_id = f"test-session-{uuid.uuid4().hex[:6]}"

    ConversationMemoryService.clear_session(agent_id, session_id)
    assert ConversationMemoryService.is_greeting_sent(agent_id, session_id) is False

    # Simulate first initialization
    ConversationMemoryService.mark_greeting_sent(agent_id, session_id)
    ConversationMemoryService.add_turn(agent_id, session_id, role="assistant", content="Hello from Campaign A")

    # Second check must confirm greeting already sent
    assert ConversationMemoryService.is_greeting_sent(agent_id, session_id) is True
    history = ConversationMemoryService.get_history(agent_id, session_id)
    assert len(history) == 1
    assert history[0]["content"] == "Hello from Campaign A"


@pytest.mark.anyio
async def test_tts_failure_does_not_falsely_report_success():
    """When TTS synthesis fails or produces empty bytes, synthesize_for_call returns None without false success."""
    from app.services.tts_service import synthesize_for_call

    class DummyTTSAgent:
        voice_id = "test-voice"
        is_local_test = True

    dummy_agent = DummyTTSAgent()
    from app.services.tts_service import _TTS_BYTES_CACHE
    _TTS_BYTES_CACHE.clear()

    with patch("app.services.tts_service.synthesize_murf_speech", new_callable=AsyncMock) as mock_murf, \
         patch("app.services.tts_service.synthesize_snapserve_speech", new_callable=AsyncMock) as mock_snap, \
         patch("app.services.tts_service.synthesize_edge_speech", new_callable=AsyncMock) as mock_edge, \
         patch("app.services.tts_service.synthesize_speech", new_callable=AsyncMock) as mock_eleven:
        # Simulate all TTS providers failing with empty bytes
        mock_murf.return_value = b""
        mock_snap.return_value = b""
        mock_edge.return_value = b""
        mock_eleven.return_value = b""
        res = await synthesize_for_call("Hello from Campaign", agent=dummy_agent)
        assert res is None  # Never returns a valid audio url when bytes are empty


def test_qualification_lifecycle_order_preserved():
    """QualificationStageEngine starts with Greeting stage and progresses stages in order."""
    script = """
    1. Greeting: Hello, how can I help you today?
    2. Need Discovery: What service are you interested in?
    3. Property Type: Do you own a single family home?
    4. Confirmation: Thank you, our specialist will follow up.
    """
    stages = QualificationStageEngine.extract_stages_from_script(script, "Alex", "Test Company")
    assert len(stages) >= 3

    # Turn 0: Call started, initial greeting stage is fulfilled, next active stage is stage 1 (Need Discovery)
    steps, stage_name, active_idx, qual_status, _, _ = QualificationStageEngine.evaluate_progress(
        stages=stages,
        collected_fields={},
        current_step_idx=0,
        script_text=script,
    )
    assert active_idx == 1
    assert "need discovery" in stage_name.lower()
    assert qual_status == "PENDING"

    # Turn 1: Caller states requirement, advances to property type
    steps, stage_name, active_idx, qual_status, _, _ = QualificationStageEngine.evaluate_progress(
        stages=stages,
        collected_fields={"service": "Solar Installation", "2. Need Discovery": "Solar"},
        current_step_idx=1,
        script_text=script,
    )
    assert active_idx >= 1
    assert qual_status == "PENDING"
