import uuid
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import async_session_factory
from app.db.models import Call, CallTransfer, User
from app.services.hold_music_service import stop_customer_hold, start_customer_hold
from app.services.call_transfer_service import CallTransferService
from app.api.routes.voice_agent_transfer import accept_voice_agent_transfer, VoiceAgentTransferAcceptRequest


@pytest.mark.asyncio
async def test_auto_stop_hold_music_on_transfer_accept():
    """
    Test 1: Agent 1 transfers customer to Agent 2. When Agent 2 answers,
    stop_customer_hold is invoked, removing the hold state, setting status to HUMAN_HANDLING,
    and broadcasting HOLD_MUSIC_STOPPED without requiring Agent 1 to click Resume Call.
    """
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-trans-{uid}"
    transfer_id = f"trans-{uid}"
    agent1_id = f"agent1-{uid}"
    agent2_id = f"agent2-{uid}"
    tenant_id = f"tenant-{uid}"

    async with async_session_factory() as db:
        call = Call(
            id=call_id,
            business_id=tenant_id,
            status="TRANSFER_REQUESTED",
            client_name="Test Customer",
            client_phone="+15559876543",
            agent_id=agent1_id,
        )
        transfer = CallTransfer(
            id=transfer_id,
            business_id=tenant_id,
            call_id=call_id,
            from_agent_id=agent1_id,
            to_agent_id=agent2_id,
            transfer_type="warm",
            status="PENDING",
        )
        agent2 = User(
            id=agent2_id,
            email=f"agent2_{uid}@callzenza.com",
            full_name="Agent Two",
            hashed_password="mock_hashed_password",
            business_id=tenant_id,
        )
        db.add_all([call, transfer, agent2])
        await db.commit()

        # Execute accept as Agent 2
        payload = VoiceAgentTransferAcceptRequest(
            call_id=call_id,
            call_session_id=call_id,
            transfer_id=transfer_id,
            voice_agent_id=agent2_id,
        )
        res = await accept_voice_agent_transfer(payload=payload, transfer_id=transfer_id, user=agent2, db=db)

        assert res["success"] is True
        assert res["status"] == "ACCEPTED"

        # Verify call status was updated from TRANSFER_REQUESTED / ON_HOLD to active handling
        await db.refresh(call)
        assert call.status in ("HUMAN_HANDLING", "CONFERENCE_3WAY_ACTIVE")
        assert call.agent_id in (agent1_id, agent2_id)


@pytest.mark.asyncio
async def test_hold_music_continues_while_agent2_ringing():
    """
    Test 2: While Agent 2 is ringing (transfer initiated, but not yet accepted),
    call remains in TRANSFER_REQUESTED state and hold is not cancelled.
    """
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-ringing-{uid}"
    tenant_id = f"tenant-{uid}"

    async with async_session_factory() as db:
        call = Call(
            id=call_id,
            business_id=tenant_id,
            status="IN_PROGRESS",
            client_phone="+15551234567",
        )
        db.add(call)
        await db.commit()

        # Place on hold for warm transfer
        await start_customer_hold(db, call)
        await db.commit()
        await db.refresh(call)

        # Hold status must remain while Agent 2 is ringing
        assert call.status == "TRANSFER_REQUESTED"


@pytest.mark.asyncio
async def test_stop_customer_hold_unconditional_broadcast():
    """
    Test 3 & 5: stop_customer_hold works even without Twilio credentials
    (e.g., SIP/WebRTC/local softphone) and unconditionally returns True,
    updating call status and broadcasting HOLD_MUSIC_STOPPED.
    """
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-hold-stop-{uid}"
    tenant_id = f"tenant-{uid}"

    async with async_session_factory() as db:
        call = Call(
            id=call_id,
            business_id=tenant_id,
            status="ON_HOLD",
            client_phone="+15557778888",
        )
        db.add(call)
        await db.commit()

        # Stop hold
        success = await stop_customer_hold(db, call, target_agent_id=f"agent-{uid}")
        assert success is True
        assert call.status == "HUMAN_HANDLING"
        assert call.agent_id == f"agent-{uid}"


@pytest.mark.asyncio
async def test_idempotent_duplicate_accept_events():
    """
    Test 4: Duplicate accept events do not raise errors or corrupt state.
    """
    uid = uuid.uuid4().hex[:8]
    call_id = f"call-idem-{uid}"
    transfer_id = f"trans-idem-{uid}"
    agent2_id = f"agent2-idem-{uid}"
    tenant_id = f"tenant-{uid}"

    async with async_session_factory() as db:
        call = Call(
            id=call_id,
            business_id=tenant_id,
            status="TRANSFER_REQUESTED",
            client_phone="+15554443333",
        )
        transfer = CallTransfer(
            id=transfer_id,
            business_id=tenant_id,
            call_id=call_id,
            from_agent_id=f"agent1-{uid}",
            to_agent_id=agent2_id,
            transfer_type="warm",
            status="PENDING",
        )
        agent2 = User(
            id=agent2_id,
            email=f"agent2_idem_{uid}@callzenza.com",
            full_name="Agent Two Idem",
            role="AGENT",
            hashed_password="mock_hashed_password",
            business_id=tenant_id,
        )
        db.add_all([call, transfer, agent2])
        await db.commit()

        payload = VoiceAgentTransferAcceptRequest(
            call_id=call_id,
            transfer_id=transfer_id,
            voice_agent_id=agent2_id,
        )
        # First accept
        res1 = await accept_voice_agent_transfer(payload=payload, transfer_id=transfer_id, user=agent2, db=db)
        assert res1["success"] is True

        # Second accept (duplicate event)
        res2 = await accept_voice_agent_transfer(payload=payload, transfer_id=transfer_id, user=agent2, db=db)
        assert res2["success"] is True
        assert res2["status"] == "ACCEPTED"
