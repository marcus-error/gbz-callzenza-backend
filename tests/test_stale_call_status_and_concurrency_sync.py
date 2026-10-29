import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import (
    Business,
    User,
    Lead,
    Campaign,
    CampaignLead,
    AIAgent,
    Call,
    Client,
)
from app.core.security import hash_password
from app.api.routes.campaigns import dialer_status
from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_leads
from app.api.routes.local_voice_test import start_local_test, end_local_test, StartSessionRequest, EndSessionRequest
from app.services.voice_service import SIPVoiceProvider


@pytest.fixture(autouse=True)
def mock_sip_telephony(monkeypatch):
    async def mock_place_call(*args, **kwargs):
        return f"mock-sid-{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(SIPVoiceProvider, "place_call", mock_place_call)


@pytest_asyncio.fixture
async def setup_test_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"SyncBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin = User(
            email=f"admin_sync_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("securepassword"),
            full_name="Admin Sync",
            first_name="Admin",
            last_name="Sync",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"Agent_Sync_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        db.add_all([admin, agent])
        await db.commit()
        await db.refresh(biz)
        await db.refresh(admin)
        await db.refresh(agent)
        return {"biz": biz, "admin": admin, "agent": agent}


@pytest.mark.asyncio
async def test_stale_ringing_call_auto_recovery_and_api_accuracy(setup_test_ctx):
    """
    Validates that historical calls left in RINGING with no live SIP session
    are auto-reaped cleanly, do not falsely report as RINGING in campaign AI leads,
    and dialer status reports 0 active calls.
    """
    ctx = setup_test_ctx
    biz = ctx["biz"]
    admin = ctx["admin"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Ratio 1:2 Stale Test",
            business_id=biz.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            agent_id=agent.id,
            schedule={"calling_mode": "local", "concurrency": 2},
        )
        db.add(camp)
        await db.flush()

        lead = Lead(
            business_id=biz.id,
            first_name="StaleLead",
            last_name="Test",
            phone="9876500001",
            status="NEW",
            call_status="NEW",
        )
        db.add(lead)
        await db.flush()

        cl = CampaignLead(
            campaign_id=camp.id,
            lead_id=lead.id,
            status="PENDING",
        )
        db.add(cl)
        await db.flush()

        # Insert a historical call that was initiated 5 minutes ago and abandoned in RINGING
        five_mins_ago = datetime.utcnow() - timedelta(minutes=5)
        stale_call = Call(
            id=str(uuid.uuid4()),
            business_id=biz.id,
            campaign_id=camp.id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            initiated_at=five_mins_ago,
            created_at=five_mins_ago,
            ended_at=None,
        )
        db.add(stale_call)
        await db.commit()

        # 1. Fetch dialer status — should auto-clean stale call and report 0 active
        d_status = await dialer_status(id=camp.id, user=admin, db=db)
        assert d_status["active_calls"] == 0, f"Expected 0 active calls, got {d_status['active_calls']}"
        assert d_status["ratio"] == 2
        assert d_status["target_capacity"] == 2

        # 2. Fetch campaign AI agent leads — lead should report PENDING, not RINGING
        ai_leads = await get_campaign_ai_agent_leads(campaign_id=camp.id, user=admin, db=db)
        assert len(ai_leads["leads"]) == 1
        lead_data = ai_leads["leads"][0]
        assert lead_data["call_status"] == "PENDING", f"Expected PENDING, got {lead_data['call_status']}"

        # 3. Verify the Call record in DB was transitioned out of RINGING
        db_call = await db.get(Call, stale_call.id)
        assert db_call.status in ("COMPLETED", "FAILED")
        assert db_call.ended_at is not None


@pytest.mark.asyncio
async def test_concurrency_limit_1_2_enforced_on_outbound_dispatch(setup_test_ctx):
    """
    Validates that for ratio 1:2 (capacity 2), starting 2 concurrent calls succeeds,
    and attempting a 3rd call raises HTTP 429 concurrency limit reached.
    """
    ctx = setup_test_ctx
    biz = ctx["biz"]
    admin = ctx["admin"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Ratio 1:2 Concurrency Enforce",
            business_id=biz.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            agent_id=agent.id,
            schedule={"calling_mode": "local", "concurrency": 2},
        )
        db.add(camp)
        await db.flush()

        leads = []
        for i in range(3):
            ld = Lead(
                business_id=biz.id,
                first_name=f"LeadConc_{i}",
                last_name="Test",
                phone=f"987654321{i}",
                status="NEW",
                call_status="NEW",
            )
            db.add(ld)
            await db.flush()
            cl = CampaignLead(campaign_id=camp.id, lead_id=ld.id, status="PENDING")
            db.add(cl)
            leads.append(ld)
        await db.commit()

        # Start call 1 (Slot 1/2)
        resp1 = await start_local_test(
            body=StartSessionRequest(campaign_id=camp.id, lead_id=leads[0].id),
            db=db,
        )
        assert resp1["call_id"] is not None

        # Start call 2 (Slot 2/2)
        resp2 = await start_local_test(
            body=StartSessionRequest(campaign_id=camp.id, lead_id=leads[1].id),
            db=db,
        )
        assert resp2["call_id"] is not None

        # Attempt to start call 3 (Slot 3/2 - must be blocked with 429)
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(
                body=StartSessionRequest(campaign_id=camp.id, lead_id=leads[2].id),
                db=db,
            )
        assert exc_info.value.status_code == 429
        assert "concurrency limit reached" in exc_info.value.detail.lower()

        # Clean up active calls
        await end_local_test(body=EndSessionRequest(session_id=resp1["session_id"]), db=db)
        await end_local_test(body=EndSessionRequest(session_id=resp2["session_id"]), db=db)


@pytest.mark.asyncio
async def test_duplicate_lead_dialing_prevention(setup_test_ctx):
    """
    Validates that a lead already in an active call cannot be dialed again
    by a concurrent or repeated request, returning HTTP 409 Conflict.
    """
    ctx = setup_test_ctx
    biz = ctx["biz"]
    admin = ctx["admin"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Duplicate Lead Guard",
            business_id=biz.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            agent_id=agent.id,
            schedule={"calling_mode": "local", "concurrency": 2},
        )
        db.add(camp)
        await db.flush()

        lead = Lead(
            business_id=biz.id,
            first_name="DuplicateTarget",
            last_name="Test",
            phone="9876549999",
            status="NEW",
            call_status="NEW",
        )
        db.add(lead)
        await db.flush()
        cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
        db.add(cl)
        await db.commit()

        # Dispatch call for lead
        resp = await start_local_test(
            body=StartSessionRequest(campaign_id=camp.id, lead_id=lead.id),
            db=db,
        )
        assert resp["session_id"] is not None

        # Attempt duplicate call for the SAME lead while active
        with pytest.raises(HTTPException) as exc_info:
            await start_local_test(
                body=StartSessionRequest(campaign_id=camp.id, lead_id=lead.id),
                db=db,
            )
        assert exc_info.value.status_code == 409
        assert "already in progress" in exc_info.value.detail.lower()

        # Clean up
        await end_local_test(body=EndSessionRequest(session_id=resp["session_id"]), db=db)


@pytest.mark.asyncio
async def test_termination_synchronization_clears_ringing_state(setup_test_ctx):
    """
    Validates that ending a call synchronizes terminal COMPLETED status to
    Call, Lead, and CampaignLead, and dialer_status immediately drops active count to 0.
    """
    ctx = setup_test_ctx
    biz = ctx["biz"]
    admin = ctx["admin"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Termination Sync Campaign",
            business_id=biz.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            agent_id=agent.id,
            schedule={"calling_mode": "local", "concurrency": 2},
        )
        db.add(camp)
        await db.flush()

        lead = Lead(
            business_id=biz.id,
            first_name="TermSyncLead",
            last_name="Test",
            phone="9876548888",
            status="NEW",
            call_status="NEW",
        )
        db.add(lead)
        await db.flush()
        cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
        db.add(cl)
        await db.commit()

        # Start call
        resp = await start_local_test(
            body=StartSessionRequest(campaign_id=camp.id, lead_id=lead.id),
            db=db,
        )
        call_id = resp["call_id"]

        # Check in-progress status
        d_status = await dialer_status(id=camp.id, user=admin, db=db)
        assert d_status["active_calls"] == 1

        # Terminate call cleanly
        await end_local_test(
            body=EndSessionRequest(session_id=resp["session_id"], duration_seconds=12),
            db=db,
        )

        # Verify DB records
        call_rec = await db.get(Call, call_id)
        assert call_rec.status == "COMPLETED"
        assert call_rec.ended_at is not None

        lead_rec = await db.get(Lead, lead.id)
        assert lead_rec.call_status == "COMPLETED"

        cl_rec = (await db.execute(select(CampaignLead).where(CampaignLead.id == cl.id))).scalars().first()
        assert cl_rec.status in ("COMPLETED", "QUALIFIED")

        # Verify dialer_status drops to 0 active
        d_status_after = await dialer_status(id=camp.id, user=admin, db=db)
        assert d_status_after["active_calls"] == 0
