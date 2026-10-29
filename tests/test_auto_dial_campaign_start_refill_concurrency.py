import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
from sqlalchemy import select, func
from fastapi import BackgroundTasks

from app.db.database import AsyncSessionLocal
from app.db.models import (
    Business,
    User,
    Lead,
    Campaign,
    CampaignLead,
    AIAgent,
    Call,
    DialerRun,
    DialJob,
    Client,
)
from app.core.security import hash_password
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES, PENDING_STATUSES
from app.api.routes.campaigns import start_campaign, pause_campaign, dialer_status, start_dialer
from app.api.routes.local_voice_test import end_local_test, EndSessionRequest, local_test_sessions


@pytest_asyncio.fixture
async def setup_auto_dial_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"AutoDialBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin = User(
            email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("password123"),
            full_name="Admin User",
            first_name="Admin",
            last_name="User",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"Agent_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        client = Client(
            business_id=biz.id,
            name="Test Client",
            company_name="Test Corp",
            contact_number="9876543210",
        )
        db.add_all([admin, agent, client])
        await db.commit()

        yield {
            "biz_id": biz.id,
            "admin": admin,
            "agent_id": agent.id,
            "client_id": client.id,
        }


@pytest.mark.asyncio
async def test_button_endpoint_start_and_pause_status_transitions(setup_auto_dial_ctx):
    """
    Test 1: Frontend Auto-Dial (1:3) button endpoints:
    - POST /api/campaigns/{id}/start transitions campaign to RUNNING and sets auto_dial_enabled=True.
    - If all leads were previously CALLED or COMPLETED, start_campaign resets eligible leads to PENDING
      so that the dialer finds pending leads and does NOT prematurely mark campaign COMPLETED.
    - POST /api/campaigns/{id}/pause transitions campaign to PAUSED.
    """
    ctx = setup_auto_dial_ctx

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Button Endpoint Test Campaign",
            business_id=ctx["biz_id"],
            client_id=ctx["client_id"],
            client_name="Test Client",
            status="COMPLETED",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=False,
            auto_dial_ratio=3,
        )
        db.add(camp)
        await db.flush()

        leads = [
            Lead(
                id=str(uuid.uuid4()),
                first_name=f"Lead_{i}",
                last_name="Test",
                phone=f"+91987654321{i}",
                business_id=ctx["biz_id"],
                status="CALLED",
            )
            for i in range(3)
        ]
        db.add_all(leads)
        await db.flush()

        for lead in leads:
            cl = CampaignLead(
                campaign_id=camp.id,
                lead_id=lead.id,
                status="CALLED",
                attempt_count=1,
            )
            db.add(cl)
        await db.commit()

        # Execute start_campaign endpoint (as triggered by onClick of Auto-Dial button)
        bg = BackgroundTasks()
        started_camp = await start_campaign(id=camp.id, background_tasks=bg, user=ctx["admin"], db=db)
        assert started_camp.status == "RUNNING"
        assert started_camp.auto_dial_enabled is True

        # Verify leads are reset to PENDING and eligible for dialer
        pending_cnt = await db.scalar(
            select(func.count(CampaignLead.id)).where(
                CampaignLead.campaign_id == camp.id,
                CampaignLead.status.in_(PENDING_STATUSES),
            )
        )
        assert pending_cnt == 3

        # Execute pause_campaign endpoint
        paused_camp = await pause_campaign(id=camp.id, user=ctx["admin"], db=db)
        assert paused_camp.status == "PAUSED"


@pytest.mark.asyncio
async def test_ending_local_voice_test_does_not_trigger_campaign_refill(setup_auto_dial_ctx, monkeypatch):
    """
    Test 2: Ending a local voice test session in LocalVoiceTestModal
    must NOT trigger campaign refill.
    """
    ctx = setup_auto_dial_ctx
    refill_calls = []

    def fake_refill(campaign_id: str):
        refill_calls.append(campaign_id)

    monkeypatch.setattr("app.workers.tasks.trigger_campaign_refill", fake_refill)

    session_id = f"test-session-{uuid.uuid4().hex[:8]}"
    camp_id = str(uuid.uuid4())

    local_test_sessions[session_id] = {
        "campaign_id": camp_id,
        "lead_id": str(uuid.uuid4()),
        "agent_id": ctx["agent_id"],
        "started_at": datetime.utcnow().timestamp(),
        "messages": [],
    }

    async with AsyncSessionLocal() as db:
        req = EndSessionRequest(session_id=session_id)
        resp = await end_local_test(req, db=db)
        assert resp["status"] == "success"

    # Crucial assertion: local voice test end must NOT trigger campaign refill!
    assert len(refill_calls) == 0


@pytest.mark.asyncio
async def test_ratio_1_3_initiates_three_distinct_concurrent_outbound_calls(setup_auto_dial_ctx, monkeypatch):
    """
    Test 3: For a 1:3 ratio, campaign dialer must reserve distinct eligible leads
    and initiate exactly three concurrent outbound calls.
    """
    ctx = setup_auto_dial_ctx
    dispatched_calls = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        call_id = f"call-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=call_id,
            business_id=lead.business_id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+1800555000",
            to_number=lead.phone,
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()

        from sqlalchemy import update
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.lead_id == lead.id)
            .values(status="RINGING", call_id=call_id)
        )
        dispatched_calls.append({"lead_id": lead.id, "call_id": call_id, "to": lead.phone})
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="1:3 Three Concurrent Calls Test Campaign",
            business_id=ctx["biz_id"],
            client_id=ctx["client_id"],
            client_name="Test Client",
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=3,
            schedule={"calling_mode": "local"},
        )
        db.add(camp)
        await db.flush()

        leads = [
            Lead(
                id=str(uuid.uuid4()),
                first_name=f"Customer_{i}",
                last_name="Doe",
                phone=f"+91987654320{i}",
                business_id=ctx["biz_id"],
                status="PENDING",
            )
            for i in range(5)
        ]
        db.add_all(leads)
        await db.flush()

        for lead in leads:
            cl = CampaignLead(
                campaign_id=camp.id,
                lead_id=lead.id,
                status="PENDING",
            )
            db.add(cl)
        await db.commit()

        # Run predictive dialer for campaign
        await run_predictive_dialer_async(camp.id)

        # 1. Exactly 3 distinct calls initiated
        assert len(dispatched_calls) == 3
        distinct_lead_ids = set(c["lead_id"] for c in dispatched_calls)
        assert len(distinct_lead_ids) == 3
        distinct_call_ids = set(c["call_id"] for c in dispatched_calls)
        assert len(distinct_call_ids) == 3

        # 2. Check dialer status endpoint reflects 3 active calls, 0 available slots, 2 pending leads
        st = await dialer_status(id=camp.id, user=ctx["admin"], db=db)
        assert st["status"] == "RUNNING"
        assert st["ratio"] == 3
        assert st["target_capacity"] == 3
        assert st["active_calls"] == 3
        assert st["available_slots"] == 0
        assert st["pending_leads"] == 2

        # 3. Simulate 1 call completing -> Refill cycle initiates 1 more call
        completed_call_id = dispatched_calls[0]["call_id"]
        from sqlalchemy import update
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.call_id == completed_call_id)
            .values(status="COMPLETED")
        )
        await db.execute(
            update(Call)
            .where(Call.id == completed_call_id)
            .values(status="COMPLETED", ended_at=datetime.utcnow())
        )
        await db.commit()

        # Trigger dialer refill
        await run_predictive_dialer_async(camp.id)

        # Total dispatched calls should now be 4 (3 initial + 1 refilled)
        assert len(dispatched_calls) == 4
        assert len(set(c["lead_id"] for c in dispatched_calls)) == 4

        # Dialer status now has 3 active calls and 1 pending lead remaining
        st_refilled = await dialer_status(id=camp.id, user=ctx["admin"], db=db)
        assert st_refilled["status"] == "RUNNING"
        assert st_refilled["active_calls"] == 3
        assert st_refilled["available_slots"] == 0
        assert st_refilled["pending_leads"] == 1
