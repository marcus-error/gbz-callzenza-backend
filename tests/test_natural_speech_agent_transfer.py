import pytest
import pytest_asyncio
import uuid
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import User, Call, Lead, Expert
from app.services.human_handoff_intent import HumanHandoffIntentDetector
from app.services.call_transfer_service import CallTransferService
from app.repositories.call_transfer_repository import CallTransferRepository


# =====================================================================
# 1. NATURAL SPEECH INTENT DETECTION & FALSE POSITIVE GUARDS
# =====================================================================

def test_user_required_natural_speech_phrases():
    """Verify all 6 customer speech examples from requirement."""
    required_phrases = [
        "I want to talk to an agent.",
        "Can you connect me to a human?",
        "Please transfer me to your representative.",
        "I need to speak with customer support.",
        "Can I talk to someone?",
        "I would like to speak to a real person.",
    ]
    for phrase in required_phrases:
        res = HumanHandoffIntentDetector.detect(phrase)
        assert res.has_intent is True, f"Failed detecting intent for: '{phrase}'"
        assert res.confidence >= 0.85, f"Low confidence ({res.confidence}) for: '{phrase}'"
        assert res.requires_confirmation is False, f"Unexpected confirmation requirement for: '{phrase}'"


def test_natural_speech_variations_and_multilingual():
    """Verify variations, Tamil, Tanglish, and Hindi speech."""
    variations = [
        ("I need to talk to customer support right now", "en"),
        ("Please switch me to a live agent", "en"),
        ("Can you put me through to a real person?", "en"),
        ("agent kitta pesanum", "ta"),
        ("manushan kitta pesanum", "ta"),
        ("agent kita connect panu", "ta"),
        ("mujhe agent se baat karni hai", "hi"),
        ("kisi insaan se baat karao", "hi"),
        ("customer care se baat karni hai", "hi"),
    ]
    for text, expected_lang in variations:
        res = HumanHandoffIntentDetector.detect(text)
        assert res.has_intent is True, f"Failed detecting intent for: '{text}'"
        assert res.language == expected_lang, f"Expected lang {expected_lang}, got {res.language} for '{text}'"


def test_negative_guards_do_not_trigger_transfer():
    """Inquiries containing 'agent' or 'bot' without transfer intent must not trigger transfer."""
    false_positive_queries = [
        "What does an insurance agent do?",
        "What is the agent fee?",
        "What is the agent commission on this plan?",
        "Are you an AI agent or a real person?",
        "Is this an automated agent?",
        "What kind of bot are you?",
        "Who is the agent on my policy?",
        "Tell me about the maturity benefit and lump sum",
    ]
    for query in false_positive_queries:
        res = HumanHandoffIntentDetector.detect(query)
        assert res.has_intent is False, f"Negative guard failed for: '{query}'"


# =====================================================================
# 2. TRANSFER ORCHESTRATION, IDEMPOTENCY & AGENT RESERVATION
# =====================================================================

@pytest.mark.asyncio
async def test_transfer_with_available_agent_and_idempotency():
    """
    When Voice Agent is available:
    1. check_and_process_customer_utterance reserves the agent.
    2. Hold message is returned.
    3. Duplicate/repeated speech utterance is safely ignored (idempotent).
    """
    uid = uuid.uuid4().hex[:8]
    agent_id = f"test-human-{uid}"
    call_id = f"call-speech-{uid}"

    async with AsyncSessionLocal() as db:
        # Create available Voice Agent
        va = User(
            id=agent_id,
            email=f"human_{uid}@callzenza.com",
            hashed_password="pwd",
            full_name=f"Human Specialist {uid}",
            role="VOICE_AGENT",
            status="AVAILABLE",
            business_id="default",
        )
        db.add(va)

        # Create active Call
        call = Call(
            id=call_id,
            business_id="default",
            status="IN_PROGRESS",
            client_name="Test Customer",
            client_phone="+15551234567",
        )
        db.add(call)
        await db.commit()

        session = {
            "session_id": call_id,
            "call_id": call_id,
            "business_id": "default",
            "customer_name": "Test Customer",
            "history": [],
            "is_transferred": False,
        }

        # First customer utterance requesting agent
        res1 = await CallTransferService.check_and_process_customer_utterance(
            session=session,
            user_text="I want to talk to an agent.",
            db=db,
        )
        assert res1 is not None
        assert res1["is_transferred"] is True
        assert res1["transfer_status"] == "RINGING"
        assert res1["assigned_agent_name"] in res1["response"]
        assert session["is_transferred"] is True

        # Second utterance (repeated transcript event or user repeating) -> MUST be ignored
        res2 = await CallTransferService.check_and_process_customer_utterance(
            session=session,
            user_text="I want to talk to an agent.",
            db=db,
        )
        assert res2 is None, "Repeated utterance must be ignored due to idempotency guard"


@pytest.mark.asyncio
async def test_transfer_fallback_when_no_agent_available():
    """
    When no Voice Agent is available:
    1. Return is_fallback=True.
    2. Call is not dropped.
    3. Fallback message informs customer that specialists are busy.
    """
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-fallback-{uid}"

    async with AsyncSessionLocal() as db:
        # Call in isolated business with zero available agents
        biz_id = f"isolated-biz-{uid}"
        call = Call(
            id=call_id,
            business_id=biz_id,
            status="IN_PROGRESS",
            client_name="Fallback Customer",
        )
        db.add(call)
        await db.commit()

        session = {
            "session_id": call_id,
            "call_id": call_id,
            "business_id": biz_id,
            "customer_name": "Fallback Customer",
            "history": [],
            "is_transferred": False,
        }

        res = await CallTransferService.check_and_process_customer_utterance(
            session=session,
            user_text="Can you connect me to a human?",
            db=db,
        )
        assert res is not None
        assert res["is_fallback"] is True
        assert "specialists are currently" in res["response"]
        # In fallback, session remains active for AI to continue
        assert res["is_transferred"] is False


# =====================================================================
# 3. TALK TO AGENT BUTTON COMPATIBILITY (MANUAL & REST FLOW)
# =====================================================================

@pytest.mark.asyncio
async def test_talk_to_agent_manual_button_endpoint():
    """Existing 'Talk to Agent' manual button endpoint remains fully functional."""
    transport = ASGITransport(app=app)
    uid = uuid.uuid4().hex[:8]
    session_id = f"manual-sess-{uid}"

    async with AsyncSessionLocal() as db:
        # Prepopulate expert
        exp = Expert(
            id=f"exp-{uid}",
            name=f"Expert {uid}",
            email=f"expert_{uid}@callzenza.com",
            business_id="default",
            status="AVAILABLE",
        )
        db.add(exp)

        # Prepopulate lead
        lead = Lead(
            id=f"lead-{uid}",
            business_id="default",
            first_name="Manual",
            last_name="Tester",
            phone="+15559876543",
            status="ENGAGED",
        )
        db.add(lead)
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Call manual Talk to Agent endpoint
        res = await client.post(
            "/api/local-voice-test/talk-to-agent",
            json={
                "session_id": session_id,
                "lead_id": f"lead-{uid}",
                "notes": "Clicked manual Talk to Agent button in UI",
            },
        )
        assert res.status_code == 200
        data = res.json()
        assert data["success"] is True
        assert data["is_transferred"] is True
        assert "transferred" in data["response"].lower()
