import pytest
import pytest_asyncio
import uuid
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import settings
from app.db.database import AsyncSessionLocal
from app.db.models import User, Campaign, Call, Lead
from app.services.human_handoff_intent import (
    HumanHandoffIntentDetector,
    IntentDetectionResult,
)
from app.services.agent_availability_service import AgentAvailabilityService
from app.services.call_transfer_service import CallTransferService
from app.repositories.call_transfer_repository import (
    CallTransferRepository,
    VoiceAgentPresence,
    CallTransferSession,
)


# =====================================================================
# 1. INTENT DETECTION TESTS (English, Tamil, Tanglish, Hindi, Guards)
# =====================================================================

def test_intent_detection_multilingual():
    # English tests
    res_en = HumanHandoffIntentDetector.detect("I want to talk with agent")
    assert res_en.has_intent is True
    assert res_en.language == "en"
    assert res_en.confidence >= 0.85

    res_en2 = HumanHandoffIntentDetector.detect("Can you connect me to a person please?")
    assert res_en2.has_intent is True

    # Tamil tests
    res_ta = HumanHandoffIntentDetector.detect("agent kitta pesanum")
    assert res_ta.has_intent is True
    assert res_ta.language == "ta"

    res_ta2 = HumanHandoffIntentDetector.detect("நேரடி முகவருடன் பேச வேண்டும்")
    assert res_ta2.has_intent is True

    # Tanglish tests
    res_tanglish = HumanHandoffIntentDetector.detect("agent kita connect panu")
    assert res_tanglish.has_intent is True

    # Hindi tests
    res_hi = HumanHandoffIntentDetector.detect("mujhe agent se baat karni hai")
    assert res_hi.has_intent is True
    assert res_hi.language == "hi"

    res_hi2 = HumanHandoffIntentDetector.detect("kisi insaan se baat karao")
    assert res_hi2.has_intent is True

    # Explicit user-specified natural speech transfer requests
    phrases_to_test = [
        "I want to talk to an agent.",
        "Can you connect me to a human?",
        "Please transfer me to your representative.",
        "I need to speak with customer support.",
        "Can I talk to someone?",
        "I would like to speak to a real person.",
        "I need to talk to customer support",
        "Can you transfer me to an agent please",
    ]
    for p in phrases_to_test:
        res = HumanHandoffIntentDetector.detect(p)
        assert res.has_intent is True, f"Failed detecting intent for: '{p}'"
        assert res.confidence >= 0.85, f"Low confidence {res.confidence} for: '{p}'"

    # Negative guards: inquiries about agents, insurance, bots must NOT trigger transfer
    negative_to_test = [
        "What does an insurance agent do?",
        "Are you an AI agent or a real person?",
        "What kind of bot are you?",
        "What is the agent commission on this policy?",
        "Is there an agent fee?",
        "What are the policy benefits and lump sum?",
    ]
    for neg in negative_to_test:
        res_neg = HumanHandoffIntentDetector.detect(neg)
        assert res_neg.has_intent is False, f"Negative guard failed for: '{neg}'"


# =====================================================================
# 2. AGENT AVAILABILITY & ATOMIC LOCKING TESTS
# =====================================================================

@pytest.mark.asyncio
async def test_agent_availability_atomic_locking():
    uid = uuid.uuid4().hex[:8]
    agent_id = f"test-va-{uid}"

    async with AsyncSessionLocal() as db:
        # Create a test voice agent
        va_user = User(
            id=agent_id,
            email=f"va_{uid}@example.com",
            hashed_password="hashed_pwd",
            full_name=f"Voice Specialist {uid}",
            role="VOICE_AGENT",
            status="AVAILABLE",
            business_id="default",
        )
        db.add(va_user)
        await db.commit()

        # Step A: Lock acquisition for Call 1
        call_1 = f"call-1-{uid}"
        locked_1 = await CallTransferRepository.try_reserve_agent(
            agent_id=agent_id,
            call_id=call_1,
            ttl_seconds=20,
            db=db,
        )
        assert locked_1 is True

        # Step B: Concurrent attempt on Call 2 on SAME agent must FAIL (Atomic protection)
        call_2 = f"call-2-{uid}"
        locked_2 = await CallTransferRepository.try_reserve_agent(
            agent_id=agent_id,
            call_id=call_2,
            ttl_seconds=20,
            db=db,
        )
        assert locked_2 is False

        # Step C: Releasing Call 1 frees the agent
        await CallTransferRepository.release_agent_lock(
            agent_id=agent_id,
            call_id=call_1,
            new_status="AVAILABLE",
            db=db,
        )

        # Step D: Call 2 can now successfully acquire the lock
        locked_retry = await CallTransferRepository.try_reserve_agent(
            agent_id=agent_id,
            call_id=call_2,
            ttl_seconds=20,
            db=db,
        )
        assert locked_retry is True

        # Cleanup
        await CallTransferRepository.release_agent_lock(
            agent_id=agent_id,
            call_id=call_2,
            new_status="AVAILABLE",
            db=db,
        )


# =====================================================================
# 3. WARM TRANSFER STATE MACHINE & BRIDGE TESTS
# =====================================================================

@pytest.mark.asyncio
async def test_warm_transfer_lifecycle_and_fallback():
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-wf-{uid}"
    agent_id = f"agent-wf-{uid}"

    async with AsyncSessionLocal() as db:
        # Create voice agent
        va = User(
            id=agent_id,
            email=f"agent_{uid}@example.com",
            hashed_password="hashed_pwd",
            full_name=f"Alex Specialist {uid}",
            role="VOICE_AGENT",
            status="AVAILABLE",
            business_id="default",
        )
        db.add(va)

        # Create customer call record
        call_obj = Call(
            id=call_id,
            business_id="default",
            status="IN_PROGRESS",
            client_name="Test Customer",
            to_number="+15559876543",
        )
        db.add(call_obj)
        await db.commit()

        # Step 1: Initiate Transfer
        init_res = await CallTransferService.initiate_warm_transfer(
            call_id=call_id,
            business_id="default",
            customer_name="Test Customer",
            customer_phone="+15559876543",
            language="en",
            notes="Customer wants to speak with specialist",
            target_agent_id=agent_id,
            db=db,
        )
        assert init_res["status"] == "RINGING"
        assert init_res["is_fallback"] is False
        assert init_res["assigned_agent_id"] == agent_id
        transfer_id = init_res["transfer_id"]

        # Verify call record was updated to TRANSFER_REQUESTED without dropping
        call_check = await db.get(Call, call_id)
        assert call_check.status == "TRANSFER_REQUESTED"

        # Step 2: Voice Agent Accepts Transfer
        accept_res = await CallTransferService.accept_warm_transfer(
            transfer_id=transfer_id,
            agent_id=agent_id,
            db=db,
        )
        assert accept_res["success"] is True
        assert accept_res["status"] in ("WITH_HUMAN_AGENT", "ACCEPTED")

        # Verify call is now WITH_HUMAN_AGENT and customer leg was NEVER hung up
        call_after_accept = await db.get(Call, call_id)
        assert call_after_accept.status in ("WITH_HUMAN_AGENT", "HUMAN_HANDLING")

        # Verify agent presence is ON_CALL
        presence = await CallTransferRepository.get_presence(agent_id, db)
        assert presence.status == "ON_CALL"

        # Step 3: Complete Transfer
        comp_res = await CallTransferService.complete_warm_transfer(
            transfer_id=transfer_id,
            db=db,
        )
        assert comp_res["status"] == "COMPLETED"

        # Agent is restored to AVAILABLE
        presence_done = await CallTransferRepository.get_presence(agent_id, db)
        assert presence_done.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_warm_transfer_fallback_when_no_agent_available():
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-fallback-{uid}"

    async with AsyncSessionLocal() as db:
        # Create call
        call_obj = Call(
            id=call_id,
            business_id="biz_isolated_no_agents",
            status="IN_PROGRESS",
            client_name="Waiting Customer",
        )
        db.add(call_obj)
        await db.commit()

        # Initiate transfer in a tenant with NO available voice agents
        res = await CallTransferService.initiate_warm_transfer(
            call_id=call_id,
            business_id="biz_isolated_no_agents",
            customer_name="Waiting Customer",
            language="en",
            db=db,
        )

        # Must trigger graceful fallback without dropping call!
        assert res["is_fallback"] is True
        assert "assisting other customers" in res["message"]


# =====================================================================
# 4. FASTAPI ENDPOINT TESTS (API LAYER)
# =====================================================================

@pytest.mark.asyncio
async def test_call_transfer_api_endpoints():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Test intent endpoint
        intent_res = await client.post(
            "/api/v1/call-transfer/intent",
            json={"text": "I want to talk with agent"},
        )
        assert intent_res.status_code == 200
        data = intent_res.json()
        assert data["has_intent"] is True
        assert data["language"] == "en"

        # 2. Test Tamil intent endpoint
        intent_ta = await client.post(
            "/api/v1/call-transfer/intent",
            json={"text": "agent kitta pesanum"},
        )
        assert intent_ta.status_code == 200
        assert intent_ta.json()["has_intent"] is True
        assert intent_ta.json()["language"] == "ta"

        # 3. Test Negative identity guard
        intent_neg = await client.post(
            "/api/v1/call-transfer/intent",
            json={"text": "Are you an AI agent?"},
        )
        assert intent_neg.status_code == 200
        assert intent_neg.json()["has_intent"] is False


# =====================================================================
# 5. CALL DROP FIX & LEG PRESERVATION EDGE CASES
# =====================================================================

@pytest.mark.asyncio
async def test_customer_hangup_during_ringing_cancels_transfer():
    """Customer hangs up while transfer is ringing -> cancel releases agent lock."""
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-cancel-{uid}"
    agent_id = f"agent-cancel-{uid}"

    async with AsyncSessionLocal() as db:
        agent = User(
            id=agent_id,
            email=f"cancel_{uid}@example.com",
            hashed_password="pw",
            full_name="Cancel Test Agent",
            role="VOICE_AGENT",
            business_id="default",
            status="AVAILABLE",
        )
        db.add(agent)
        call_obj = Call(id=call_id, business_id="default", status="IN_PROGRESS", client_name="Cancelling Customer")
        db.add(call_obj)
        await db.commit()

        await AgentAvailabilityService.set_presence(agent_id, "AVAILABLE", "default", db)

        init_res = await CallTransferService.initiate_warm_transfer(
            call_id=call_id,
            business_id="default",
            customer_name="Cancelling Customer",
            target_agent_id=agent_id,
            db=db,
        )
        transfer_id = init_res["transfer_id"]

        # Agent is now RINGING or RESERVED
        presence_res = await CallTransferRepository.get_presence(agent_id, db)
        assert presence_res.status in ("RESERVED", "RINGING")

        # Customer hangs up before agent can answer
        cancel_res = await CallTransferService.cancel_warm_transfer(
            transfer_id=transfer_id,
            reason="CUSTOMER_HANGUP",
            db=db,
        )
        assert cancel_res["success"] is True
        assert cancel_res["status"] == "ABANDONED"

        # Agent lock was released back to AVAILABLE
        presence_after = await CallTransferRepository.get_presence(agent_id, db)
        assert presence_after.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_agent_accepts_after_customer_hung_up_rejects_gracefully():
    """Voice agent accepts after customer hung up -> returns CUSTOMER_ALREADY_HUNG_UP and restores agent."""
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-late-{uid}"
    agent_id = f"agent-late-{uid}"

    async with AsyncSessionLocal() as db:
        agent = User(
            id=agent_id,
            email=f"late_{uid}@example.com",
            hashed_password="pw",
            full_name="Late Agent",
            role="VOICE_AGENT",
            business_id="default",
            status="AVAILABLE",
        )
        db.add(agent)
        call_obj = Call(id=call_id, business_id="default", status="IN_PROGRESS", client_name="Impatient Customer")
        db.add(call_obj)
        await db.commit()

        await AgentAvailabilityService.set_presence(agent_id, "AVAILABLE", "default", db)

        init_res = await CallTransferService.initiate_warm_transfer(
            call_id=call_id,
            business_id="default",
            customer_name="Impatient Customer",
            target_agent_id=agent_id,
            db=db,
        )
        transfer_id = init_res["transfer_id"]

        # Customer hangs up: call status becomes COMPLETED
        call_obj.status = "COMPLETED"
        db.add(call_obj)
        await db.commit()

        # Agent tries to accept now
        accept_res = await CallTransferService.accept_warm_transfer(
            transfer_id=transfer_id,
            agent_id=agent_id,
            db=db,
        )
        assert accept_res["success"] is False
        assert accept_res["status"] == "CUSTOMER_ALREADY_HUNG_UP"

        # Agent presence was restored to AVAILABLE
        presence = await CallTransferRepository.get_presence(agent_id, db)
        assert presence.status == "AVAILABLE"


@pytest.mark.asyncio
async def test_ai_leg_disconnect_preserves_customer_leg():
    """Ending call with only_ai_leg=True when WITH_HUMAN_AGENT preserves customer call leg."""
    transport = ASGITransport(app=app)
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-preserve-{uid}"

    async with AsyncSessionLocal() as db:
        call_obj = Call(
            id=call_id,
            business_id="default",
            status="WITH_HUMAN_AGENT",
            call_sid=f"CA_mock_{uid}",
            client_name="Preserved Customer",
        )
        db.add(call_obj)
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            f"/api/v1/calls/{call_id}/end",
            json={"only_ai_leg": True},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "WITH_HUMAN_AGENT"
        assert "Customer call maintained" in data["message"]

    async with AsyncSessionLocal() as db:
        checked_call = await db.get(Call, call_id)
        assert checked_call.status == "WITH_HUMAN_AGENT"
        assert checked_call.call_sid == f"CA_mock_{uid}"

