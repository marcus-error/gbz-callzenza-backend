import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
from sqlalchemy import select, func

from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, AIAgent, Call
from app.core.security import hash_password
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES
from app.workers.tasks import trigger_campaign_refill
from app.api.routes.calls import end_call, cleanup_stale_calls
from app.api.routes.campaigns import dialer_status
from app.api.routes.voice_barge import get_active_calls as get_barge_active_calls


@pytest_asyncio.fixture
async def setup_concurrency_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"ConcBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin_user = User(
            email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Admin User",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"ConcAgent_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1"
        )
        db.add_all([admin_user, agent])
        await db.commit()

        yield {
            "biz_id": biz.id,
            "admin": admin_user,
            "agent_id": agent.id,
        }


@pytest.mark.asyncio
async def test_ratio_normalization_comprehensive():
    """Verify auto_dial_ratio accepts 1:X, X:1, plain int/str, and normalizes properly."""
    c1 = CampaignCreate(name="Camp 1", auto_dial_ratio="1:4")
    assert c1.auto_dial_ratio == 4

    c2 = CampaignCreate(name="Camp 2", auto_dial_ratio="4:1")
    assert c2.auto_dial_ratio == 4

    c3 = CampaignCreate(name="Camp 3", auto_dial_ratio="1:1")
    assert c3.auto_dial_ratio == 1

    c4 = CampaignCreate(name="Camp 4", auto_dial_ratio=" 1:6 ")
    assert c4.auto_dial_ratio == 6

    c5 = CampaignCreate(name="Camp 5", auto_dial_ratio=5)
    assert c5.auto_dial_ratio == 5

    u1 = CampaignUpdate(auto_dial_ratio="3:1")
    assert u1.auto_dial_ratio == 3

    u2 = CampaignUpdate(auto_dial_ratio="1:8")
    assert u2.auto_dial_ratio == 8


@pytest.mark.asyncio
async def test_concurrent_dialing_up_to_ratio_capacity(setup_concurrency_ctx):
    """Verify that a campaign with ratio 1:4 and 4 pending leads dials all 4 concurrently."""
    ctx = setup_concurrency_ctx
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Concurrent 1:4 Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=4,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        # Create 4 distinct leads
        leads = [
            Lead(
                first_name=f"Lead_{i}",
                phone=f"+1555000{i:04d}",
                business_id=ctx["biz_id"],
                status="PENDING"
            )
            for i in range(4)
        ]
        db.add_all(leads)
        await db.flush()

        for lead in leads:
            cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(cl)
        await db.commit()
        camp_id = camp.id

    # Run predictive dialer
    await run_predictive_dialer_async(camp_id)

    # Verify that all 4 leads were dispatched concurrently
    async with AsyncSessionLocal() as db:
        cl_stmt = select(CampaignLead).where(CampaignLead.campaign_id == camp_id)
        c_leads = (await db.scalars(cl_stmt)).all()
        assert len(c_leads) == 4
        # All 4 leads should have been dialed (RINGING or terminal)
        dialed_statuses = {"RINGING", "CALLING", "CONNECTED", "COMPLETED", "FAILED"}
        for cl in c_leads:
            assert cl.status in dialed_statuses, f"Lead {cl.id} had unexpected status: {cl.status}"
            assert cl.attempt_count >= 1
            assert cl.call_id is not None


@pytest.mark.asyncio
async def test_all_in_progress_statuses_counted_in_capacity(setup_concurrency_ctx):
    """Verify that DIALING, RINGING, CALLING, CONNECTED, IN_CALL, and RESERVED are all counted toward capacity."""
    ctx = setup_concurrency_ctx
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Status Accounting Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=6,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        statuses_to_test = ["DIALING", "RINGING", "CALLING", "CONNECTED", "IN_CALL", "RESERVED"]
        for idx, st in enumerate(statuses_to_test):
            lead = Lead(first_name=f"StatusLead_{idx}", phone=f"+1555111{idx:04d}", business_id=ctx["biz_id"], status="IN_PROGRESS")
            db.add(lead)
            await db.flush()
            cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status=st)
            db.add(cl)
        await db.commit()
        camp_id = camp.id

    # Check pacing status endpoint
    async with AsyncSessionLocal() as db:
        pacing = await dialer_status(camp_id, user=ctx["admin"], db=db)
        assert pacing["active_calls"] == 6, f"Expected 6 active calls across statuses, got {pacing['active_calls']}"
        assert pacing["available_slots"] == 0, f"Expected 0 available slots, got {pacing['available_slots']}"


@pytest.mark.asyncio
async def test_end_call_updates_campaign_lead_and_triggers_refill(setup_concurrency_ctx):
    """Verify that ending a call via end_call updates CampaignLead.status and frees capacity."""
    ctx = setup_concurrency_ctx
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="End Call Refill Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        lead1 = Lead(first_name="ActiveCustomer", phone="+15552220001", business_id=ctx["biz_id"], status="IN_PROGRESS")
        lead2 = Lead(first_name="WaitingLead", phone="+15552220002", business_id=ctx["biz_id"], status="PENDING")
        db.add_all([lead1, lead2])
        await db.flush()

        call = Call(
            campaign_id=camp.id,
            lead_id=lead1.id,
            business_id=ctx["biz_id"],
            status="RINGING",
            direction="OUTBOUND"
        )
        db.add(call)
        await db.flush()

        cl1 = CampaignLead(campaign_id=camp.id, lead_id=lead1.id, call_id=call.id, status="RINGING")
        cl2 = CampaignLead(campaign_id=camp.id, lead_id=lead2.id, status="PENDING")
        db.add_all([cl1, cl2])
        await db.commit()

        call_id = call.id
        camp_id = camp.id
        cl1_id = cl1.id
        cl2_id = cl2.id

    # Call /api/calls/{call_id}/end
    async with AsyncSessionLocal() as db:
        res = await end_call(call_id=call_id, payload=None, user=ctx["admin"], db=db)
        assert res["success"] is True

    # Verify CampaignLead status is now COMPLETED
    async with AsyncSessionLocal() as db:
        cl1_check = await db.get(CampaignLead, cl1_id)
        assert cl1_check.status == "COMPLETED", f"Expected COMPLETED, got {cl1_check.status}"

        # Capacity was freed and either available or immediately refilled by triggered refill task
        pacing = await dialer_status(camp_id, user=ctx["admin"], db=db)
        assert pacing["available_slots"] == 1 or pacing["active_calls"] == 1


@pytest.mark.asyncio
async def test_stale_cleanup_updates_campaign_lead_status(setup_concurrency_ctx):
    """Verify that cleanup_stale_calls updates CampaignLead.status to COMPLETED."""
    ctx = setup_concurrency_ctx
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Stale Cleanup Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        lead = Lead(first_name="StaleCust", phone="+15553330001", business_id=ctx["biz_id"])
        db.add(lead)
        await db.flush()

        # Call created in the past to trigger stale threshold
        past_time = datetime(2020, 1, 1, 0, 0, 0)
        call = Call(
            campaign_id=camp.id,
            lead_id=lead.id,
            business_id=ctx["biz_id"],
            status="RINGING",
            initiated_at=past_time,
            created_at=past_time
        )
        db.add(call)
        await db.flush()

        cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, call_id=call.id, status="RINGING")
        db.add(cl)
        await db.commit()
        cl_id = cl.id
        call_id = call.id

    # Run cleanup
    async with AsyncSessionLocal() as db:
        cleanup_res = await cleanup_stale_calls(stale_after_seconds=60, user=ctx["admin"], db=db)
        assert cleanup_res["cleaned"] >= 1
        assert call_id in cleanup_res["call_ids"]

    # Verify CampaignLead was updated
    async with AsyncSessionLocal() as db:
        cl_check = await db.get(CampaignLead, cl_id)
        assert cl_check.status == "COMPLETED"
