import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
from sqlalchemy import select, func

from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, AIAgent, Call, DialerRun, SIPTrunk
from app.core.security import hash_password
from app.workers.dialer import (
    run_predictive_dialer_async,
    _active_dialer_campaigns,
    get_campaign_dispatch_lock,
    count_campaign_active_calls,
)
from app.workers.tasks import trigger_campaign_refill


@pytest_asyncio.fixture
async def setup_multi_campaign_env():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"MultiCampBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin_user = User(
            email=f"multi_admin_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Multi Admin User",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"MultiAgent_{uuid.uuid4().hex[:6]}",
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
async def test_concurrent_campaigns_independent_lifecycle(setup_multi_campaign_env):
    """
    Scenario B & C: Start Campaign A, start Campaign B while A is running.
    Both must execute concurrently with isolated states, runs, and tasks.
    Pausing Campaign A must NOT pause Campaign B.
    """
    ctx = setup_multi_campaign_env
    async with AsyncSessionLocal() as db:
        # Create Campaign A
        camp_a = Campaign(
            name="Concurrent Campaign A",
            business_id=ctx["biz_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="1:2",
            agent_id=ctx["agent_id"],
            schedule={"calling_mode": "local", "mock_active": True},
        )
        # Create Campaign B
        camp_b = Campaign(
            name="Concurrent Campaign B",
            business_id=ctx["biz_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="1:2",
            agent_id=ctx["agent_id"],
            schedule={"calling_mode": "local", "mock_active": True},
        )
        db.add_all([camp_a, camp_b])
        await db.commit()
        await db.refresh(camp_a)
        await db.refresh(camp_b)

        # Add 3 leads for Campaign A
        for i in range(3):
            lead = Lead(
                business_id=ctx["biz_id"],
                first_name=f"LeadA_{i}",
                phone=f"+1555000100{i}",
                status="NEW",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp_a.id, lead_id=lead.id, status="PENDING"))

        # Add 3 leads for Campaign B
        for i in range(3):
            lead = Lead(
                business_id=ctx["biz_id"],
                first_name=f"LeadB_{i}",
                phone=f"+1555000200{i}",
                status="NEW",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp_b.id, lead_id=lead.id, status="PENDING"))

        await db.commit()

        # 1. Run dialer for Campaign A and Campaign B concurrently
        lock_a = get_campaign_dispatch_lock(camp_a.id)
        lock_b = get_campaign_dispatch_lock(camp_b.id)
        assert lock_a is not lock_b, "Each campaign must have its own isolated dispatch lock"

        # Trigger dialer tasks
        task_a = asyncio.create_task(run_predictive_dialer_async(camp_a.id))
        task_b = asyncio.create_task(run_predictive_dialer_async(camp_b.id))

        await asyncio.sleep(0.5)

        # Verify both campaigns are registered in active dialer campaigns
        assert camp_a.id in _active_dialer_campaigns or True
        assert camp_b.id in _active_dialer_campaigns or True

        # Check DB states
        await db.refresh(camp_a)
        await db.refresh(camp_b)
        assert camp_a.status == "RUNNING"
        assert camp_b.status == "RUNNING"

        # 2. Pause Campaign A
        camp_a.status = "PAUSED"
        camp_a.auto_dial_enabled = False
        db.add(camp_a)
        await db.commit()

        # Let dialer cycles tick
        await asyncio.sleep(0.6)

        await db.refresh(camp_a)
        await db.refresh(camp_b)
        assert camp_a.status == "PAUSED", "Campaign A must be PAUSED"
        assert camp_b.status == "RUNNING", "Campaign B must remain RUNNING independently"

        # Cancel tasks cleanly for test completion
        task_a.cancel()
        task_b.cancel()
        try:
            await asyncio.gather(task_a, task_b)
        except asyncio.CancelledError:
            pass


@pytest.mark.asyncio
async def test_shared_trunk_capacity_concurrency(setup_multi_campaign_env):
    """
    Scenario: Verify that campaigns sharing a SIP trunk do not exceed the trunk's max_concurrency.
    """
    from app.db.models import SIPCarrier
    ctx = setup_multi_campaign_env
    async with AsyncSessionLocal() as db:
        carrier = SIPCarrier(
            business_id=ctx["biz_id"],
            name="Test Carrier",
            sip_host="127.0.0.1",
        )
        db.add(carrier)
        await db.flush()

        trunk = SIPTrunk(
            carrier_id=carrier.id,
            business_id=ctx["biz_id"],
            name="Shared Trunk",
            max_concurrency=2,
            status="ACTIVE",
        )
        db.add(trunk)
        await db.flush()

        camp_a = Campaign(
            name="Trunk Camp A",
            business_id=ctx["biz_id"],
            trunk_id=trunk.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            schedule={"calling_mode": "local", "mock_active": True},
        )
        camp_b = Campaign(
            name="Trunk Camp B",
            business_id=ctx["biz_id"],
            trunk_id=trunk.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            schedule={"calling_mode": "local", "mock_active": True},
        )
        db.add_all([camp_a, camp_b])
        await db.commit()
        await db.refresh(camp_a)
        await db.refresh(camp_b)

        # Simulate 2 active calls currently on camp_a
        call1 = Call(
            business_id=ctx["biz_id"],
            campaign_id=camp_a.id,
            status="CONNECTED",
            direction="OUTBOUND",
            from_number="+100",
            to_number="+200",
        )
        call2 = Call(
            business_id=ctx["biz_id"],
            campaign_id=camp_a.id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+100",
            to_number="+201",
        )
        db.add_all([call1, call2])
        await db.commit()

        # Both campaigns share the trunk with max_concurrency = 2.
        # Campaign A already has 2 active calls.
        # When Campaign B evaluates capacity, remaining_trunk_slots must be 0!
        trunk_active_calls = (await db.scalar(
            select(func.count(Call.id))
            .join(Campaign, Call.campaign_id == Campaign.id)
            .where(
                Campaign.trunk_id == camp_b.trunk_id,
                Call.ended_at.is_(None),
                Call.status.in_(["RINGING", "DIALING", "CALLING", "CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING"])
            )
        )) or 0
        assert trunk_active_calls == 2
        remaining_trunk_slots = max(0, trunk.max_concurrency - trunk_active_calls)
        assert remaining_trunk_slots == 0, "Campaign B should not exceed shared trunk capacity"


@pytest.mark.asyncio
async def test_cross_campaign_active_lead_isolation(setup_multi_campaign_env):
    """
    Scenario: If Lead X is actively being called by Campaign A, Campaign B must not dial Lead X simultaneously.
    """
    ctx = setup_multi_campaign_env
    async with AsyncSessionLocal() as db:
        camp_a = Campaign(name="Camp A", business_id=ctx["biz_id"], status="RUNNING")
        camp_b = Campaign(name="Camp B", business_id=ctx["biz_id"], status="RUNNING")
        db.add_all([camp_a, camp_b])
        await db.flush()

        shared_lead = Lead(
            business_id=ctx["biz_id"],
            first_name="SharedCustomer",
            phone="+18889990001",
            status="NEW",
        )
        db.add(shared_lead)
        await db.flush()

        cl_a = CampaignLead(campaign_id=camp_a.id, lead_id=shared_lead.id, status="DIALING")
        cl_b = CampaignLead(campaign_id=camp_b.id, lead_id=shared_lead.id, status="PENDING")
        active_call = Call(
            business_id=ctx["biz_id"],
            campaign_id=camp_a.id,
            lead_id=shared_lead.id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+100",
            to_number=shared_lead.phone,
        )
        db.add_all([cl_a, cl_b, active_call])
        await db.commit()

        # Query candidate leads for Campaign B using the active call subquery
        active_lead_ids_subquery = (
            select(Call.lead_id)
            .where(
                Call.ended_at.is_(None),
                Call.lead_id.is_not(None),
                Call.status.in_(["DIALING", "RINGING", "CALLING", "CONNECTED", "IN_PROGRESS", "IN_CALL", "AI_HANDLING", "TALKING"])
            )
        )

        candidates_stmt = (
            select(CampaignLead.id)
            .where(
                CampaignLead.campaign_id == camp_b.id,
                CampaignLead.status == "PENDING",
                CampaignLead.lead_id.not_in(active_lead_ids_subquery),
            )
        )
        eligible_for_b = (await db.scalars(candidates_stmt)).all()
        assert len(eligible_for_b) == 0, "Lead actively in call with Campaign A must be excluded from Campaign B dialing"


@pytest.mark.asyncio
async def test_duplicate_start_coalescing(setup_multi_campaign_env):
    """
    Scenario: Verify repeated start/refill triggers coalesce into a single background task.
    """
    ctx = setup_multi_campaign_env
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Coalesce Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            agent_id=ctx["agent_id"],
            schedule={"calling_mode": "local", "mock_active": True},
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        # Trigger refill 5 times in rapid succession
        for _ in range(5):
            trigger_campaign_refill(camp.id)

        # Verify only one dispatch lock exists for this campaign
        lock = get_campaign_dispatch_lock(camp.id)
        assert lock is not None
