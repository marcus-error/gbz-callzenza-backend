import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone, timedelta
from sqlalchemy import select, func, update

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
from app.workers.dialer import (
    count_campaign_active_calls,
    run_predictive_dialer_async,
    ACTIVE_DIALER_STATUSES,
    PENDING_STATUSES,
)
from app.services.voice_service import (
    ACTIVE_SIP_CALLS,
    hangup_sip_call,
    reconcile_unended_call,
    is_call_genuinely_active_sip,
)
from app.workers.tasks import trigger_campaign_refill


@pytest_asyncio.fixture
async def setup_stale_test_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"StaleBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin = User(
            email=f"admin_stale_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("securepassword"),
            full_name="Admin Stale",
            first_name="Admin",
            last_name="Stale",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"Agent_Stale_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        client = Client(
            business_id=biz.id,
            name="Stale Test Client",
            company_name="Enterprise Corp",
            contact_number="9876543210",
        )
        db.add_all([admin, agent, client])
        await db.commit()

        yield {
            "business_id": biz.id,
            "admin": admin,
            "agent": agent,
            "client": client,
        }


@pytest.mark.asyncio
async def test_four_stale_records_reconciled_and_one_genuine_call_counted(setup_stale_test_ctx):
    """
    Proves:
    1. Four confirmed stale records do not permanently consume four concurrency slots.
    2. One genuine connected call remains counted while four stale records are reconciled.
    7. Reconciliation is idempotent.
    8. Duplicate call/lead records do not inflate active counts.
    """
    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            business_id=biz_id,
            name="Stale Test 1:5",
            agent_id=agent.id,
            agent_type="AI_VOICE",
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="5",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        # Create 5 leads
        leads = []
        for i in range(5):
            ld = Lead(
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="Test",
                phone=f"+9198765000{i:02d}",
                status="NEW",
            )
            db.add(ld)
            leads.append(ld)
        await db.commit()

        # Lead 0: Genuine live call with active SIP registry entry
        live_call = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            agent_id=agent.id,
            lead_id=leads[0].id,
            call_sid=f"live-call-{uuid.uuid4().hex[:6]}",
            status="CONNECTED",
            initiated_at=datetime.utcnow() - timedelta(seconds=20),
        )
        db.add(live_call)
        await db.flush()

        cl0 = CampaignLead(campaign_id=camp.id, lead_id=leads[0].id, call_id=live_call.id, status="CONNECTED")
        # Add a duplicate CampaignLead referencing the same live call/lead to test deduplication
        cl0_dup = CampaignLead(campaign_id=camp.id, lead_id=leads[0].id, call_id=live_call.id, status="CONNECTED")

        # Mock genuine active SIP socket in ACTIVE_SIP_CALLS
        ACTIVE_SIP_CALLS[live_call.id] = {
            "call_active": [True],
            "sock_closed": False,
            "call_cid": live_call.id,
            "db_call_id": live_call.id,
        }

        # Lead 1: Stale call already marked COMPLETED in Call table but stuck in CampaignLead
        stale_call_1 = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            agent_id=agent.id,
            lead_id=leads[1].id,
            status="COMPLETED",
            ended_at=datetime.utcnow() - timedelta(minutes=5),
            initiated_at=datetime.utcnow() - timedelta(minutes=10),
        )
        db.add(stale_call_1)
        await db.flush()
        cl1 = CampaignLead(campaign_id=camp.id, lead_id=leads[1].id, call_id=stale_call_1.id, status="CONNECTED")

        # Lead 2: Stale call marked FAILED in Call table but stuck in CampaignLead as RINGING
        stale_call_2 = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            agent_id=agent.id,
            lead_id=leads[2].id,
            status="FAILED",
            ended_at=datetime.utcnow() - timedelta(minutes=3),
            initiated_at=datetime.utcnow() - timedelta(minutes=8),
        )
        db.add(stale_call_2)
        await db.flush()
        cl2 = CampaignLead(campaign_id=camp.id, lead_id=leads[2].id, call_id=stale_call_2.id, status="RINGING")

        # Lead 3: Stale RINGING call with age > 45s and NO active SIP session
        stale_call_3 = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            agent_id=agent.id,
            lead_id=leads[3].id,
            status="RINGING",
            initiated_at=datetime.utcnow() - timedelta(minutes=2),
        )
        db.add(stale_call_3)
        await db.flush()
        cl3 = CampaignLead(campaign_id=camp.id, lead_id=leads[3].id, call_id=stale_call_3.id, status="RINGING")

        # Lead 4: Stale abandoned CONNECTED call with age > 180s and NO active SIP session
        stale_call_4 = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            agent_id=agent.id,
            lead_id=leads[4].id,
            status="CONNECTED",
            initiated_at=datetime.utcnow() - timedelta(minutes=5),
        )
        db.add(stale_call_4)
        await db.flush()
        cl4 = CampaignLead(campaign_id=camp.id, lead_id=leads[4].id, call_id=stale_call_4.id, status="CONNECTED")

        db.add_all([cl0, cl0_dup, cl1, cl2, cl3, cl4])
        await db.commit()

        # Execute authoritative active call counting and reconciliation
        authoritative_count, breakdown = await count_campaign_active_calls(db, camp.id)

        # 1 genuine live call + 4 stale reconciled + 1 duplicate deduplicated
        assert authoritative_count == 1, f"Expected 1 active call, got {authoritative_count}"
        assert breakdown.get("CONNECTED") == 1

        # Check DB states of stale records
        cl1_check = await db.get(CampaignLead, cl1.id)
        assert cl1_check.status == "COMPLETED"

        cl2_check = await db.get(CampaignLead, cl2.id)
        assert cl2_check.status == "FAILED"

        cl3_check = await db.get(CampaignLead, cl3.id)
        assert cl3_check.status == "NO_ANSWER"

        cl4_check = await db.get(CampaignLead, cl4.id)
        assert cl4_check.status == "COMPLETED"

        # Idempotency check: run count_campaign_active_calls again
        second_count, second_bd = await count_campaign_active_calls(db, camp.id)
        assert second_count == 1
        assert second_bd.get("CONNECTED") == 1

    # Cleanup mock registry
    ACTIVE_SIP_CALLS.pop(live_call.id, None)


@pytest.mark.asyncio
async def test_ratio_1_to_5_dispatches_four_slots_when_one_live(setup_stale_test_ctx, monkeypatch):
    """
    Proves:
    3. A 1:5 campaign with one live call and sufficient eligible leads can dispatch up to four additional calls.
    4. The campaign never exceeds its effective concurrency limit under concurrent dispatch requests.
    """
    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]
    mock_calls_created = []

    async def mock_initiate(db, lead, agent, campaign=None, campaign_id=None, force_mock=False):
        cid = f"call-1-5-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=cid,
            call_sid=f"mock-sid-{cid}",
            business_id=biz_id,
            campaign_id=campaign_id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()
        ACTIVE_SIP_CALLS[cid] = {
            "call_active": [True],
            "sock_closed": False,
            "call_cid": cid,
            "db_call_id": cid,
        }
        mock_calls_created.append(cid)
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            business_id=biz_id,
            name="Ratio 1:5 Dispatch Test",
            agent_id=agent.id,
            agent_type="AI_VOICE",
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="5",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        # 1 live call
        live_lead = Lead(business_id=biz_id, first_name="Live", phone="+919876540001", status="NEW")
        db.add(live_lead)
        await db.flush()

        live_call = Call(
            business_id=biz_id,
            campaign_id=camp.id,
            lead_id=live_lead.id,
            agent_id=agent.id,
            status="CONNECTED",
            initiated_at=datetime.utcnow() - timedelta(seconds=15),
        )
        db.add(live_call)
        await db.flush()

        cl_live = CampaignLead(campaign_id=camp.id, lead_id=live_lead.id, call_id=live_call.id, status="CONNECTED")
        db.add(cl_live)

        ACTIVE_SIP_CALLS[live_call.id] = {
            "call_active": [True],
            "sock_closed": False,
            "call_cid": live_call.id,
            "db_call_id": live_call.id,
        }

        # 10 pending eligible leads
        for i in range(10):
            p_lead = Lead(business_id=biz_id, first_name=f"Pending_{i}", phone=f"+9198765410{i:02d}", status="NEW")
            db.add(p_lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=p_lead.id, status="PENDING"))
        await db.commit()

        # Run predictive dialer
        await run_predictive_dialer_async(camp.id)

        # Check total active calls
        active_count, breakdown = await count_campaign_active_calls(db, camp.id)
        # Total limit is 5. With 1 live call, exactly 4 additional leads should have been dispatched.
        assert active_count == 5, f"Expected exactly 5 concurrent calls, got {active_count} (breakdown={breakdown})"
        assert breakdown.get("CONNECTED", 0) >= 1
        assert (breakdown.get("RINGING", 0) + breakdown.get("DIALING", 0) + breakdown.get("RESERVED", 0)) == 4

    ACTIVE_SIP_CALLS.pop(live_call.id, None)
    for mc in mock_calls_created:
        ACTIVE_SIP_CALLS.pop(mc, None)


@pytest.mark.asyncio
async def test_campaign_pause_prevents_refill(setup_stale_test_ctx):
    """
    Proves:
    9. Intentional campaign pauses prevent refill.
    """
    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            business_id=biz_id,
            name="Paused Campaign Test",
            agent_id=agent.id,
            agent_type="AI_VOICE",
            status="PAUSED",
            auto_dial_enabled=False,
            auto_dial_ratio="5",
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        lead = Lead(business_id=biz_id, first_name="PendingP", phone="+919876549999", status="NEW")
        db.add(lead)
        await db.flush()
        cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
        db.add(cl)
        await db.commit()

        # Run dialer on paused campaign
        await run_predictive_dialer_async(camp.id)

        # Lead must remain PENDING
        cl_check = await db.get(CampaignLead, cl.id)
        assert cl_check.status == "PENDING"


@pytest.mark.asyncio
async def test_ratio_1_1_and_1_3_configured_limits(setup_stale_test_ctx, monkeypatch):
    """
    Proves:
    1. Ratio 1:1 dispatches at most one call.
    2. Ratio 1:3 dispatches exactly 3 calls.
    """
    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]

    mock_calls = []
    async def mock_initiate(db, lead, ag, campaign=None, campaign_id=None, force_mock=False):
        cid = f"call-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=cid,
            business_id=biz_id,
            campaign_id=campaign_id,
            lead_id=lead.id,
            agent_id=ag.id if ag else None,
            status="RINGING",
            call_sid=f"sid-{cid}",
            direction="OUTBOUND",
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()
        ACTIVE_SIP_CALLS[cid] = {"call_active": [True], "sock_closed": False, "call_cid": cid, "db_call_id": cid}
        mock_calls.append(cid)
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)

    # Test Ratio 1:1
    async with AsyncSessionLocal() as db:
        camp_1 = Campaign(
            business_id=biz_id,
            name="Ratio 1:1 Test",
            agent_id=agent.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="1:1",
        )
        db.add(camp_1)
        await db.commit()
        await db.refresh(camp_1)

        for i in range(5):
            ld = Lead(business_id=biz_id, first_name=f"L1_{i}", phone=f"+91987655000{i}", status="NEW")
            db.add(ld)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp_1.id, lead_id=ld.id, status="PENDING"))
        await db.commit()

        await run_predictive_dialer_async(camp_1.id)
        cnt1, _ = await count_campaign_active_calls(db, camp_1.id)
        assert cnt1 == 1, f"Expected 1 active call for ratio 1:1, got {cnt1}"

    # Test Ratio 1:3
    async with AsyncSessionLocal() as db:
        camp_3 = Campaign(
            business_id=biz_id,
            name="Ratio 1:3 Test",
            agent_id=agent.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="1:3",
        )
        db.add(camp_3)
        await db.commit()
        await db.refresh(camp_3)

        for i in range(5):
            ld = Lead(business_id=biz_id, first_name=f"L3_{i}", phone=f"+91987655100{i}", status="NEW")
            db.add(ld)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp_3.id, lead_id=ld.id, status="PENDING"))
        await db.commit()

        await run_predictive_dialer_async(camp_3.id)
        cnt3, _ = await count_campaign_active_calls(db, camp_3.id)
        assert cnt3 == 3, f"Expected 3 active calls for ratio 1:3, got {cnt3}"

    for mc in mock_calls:
        ACTIVE_SIP_CALLS.pop(mc, None)


@pytest.mark.asyncio
async def test_trunk_concurrency_ceiling_enforced(setup_stale_test_ctx, monkeypatch):
    """
    Proves:
    12. Provider or trunk limits lower than the campaign ratio are respected.
    If campaign ratio is 5 but carrier trunk max_concurrency is 2, dialer dispatches at most 2.
    """
    from app.db.models import SIPTrunk

    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]

    mock_calls = []
    async def mock_initiate(db, lead, ag, campaign=None, campaign_id=None, force_mock=False):
        cid = f"call-trunk-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=cid,
            business_id=biz_id,
            campaign_id=campaign_id,
            lead_id=lead.id,
            agent_id=ag.id if ag else None,
            status="RINGING",
            call_sid=f"sid-{cid}",
            direction="OUTBOUND",
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()
        ACTIVE_SIP_CALLS[cid] = {"call_active": [True], "sock_closed": False, "call_cid": cid, "db_call_id": cid}
        mock_calls.append(cid)
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        from app.db.models import SIPCarrier
        carrier = SIPCarrier(
            business_id=biz_id,
            name="Test Carrier",
            sip_host="127.0.0.1",
            sip_port=5060,
        )
        db.add(carrier)
        await db.flush()

        trunk = SIPTrunk(
            carrier_id=carrier.id,
            business_id=biz_id,
            name="Test Restricted Trunk",
            max_concurrency=2, # Trunk capped at 2 channels!
            status="ACTIVE",
        )
        db.add(trunk)
        await db.flush()

        camp = Campaign(
            business_id=biz_id,
            name="Trunk Capped 1:5 Test",
            agent_id=agent.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="5", # Campaign desires 5, but trunk only permits 2
            trunk_id=trunk.id,
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        for i in range(6):
            ld = Lead(business_id=biz_id, first_name=f"LTrunk_{i}", phone=f"+91987655200{i}", status="NEW")
            db.add(ld)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=ld.id, status="PENDING"))
        await db.commit()

        await run_predictive_dialer_async(camp.id)
        cnt, _ = await count_campaign_active_calls(db, camp.id)
        assert cnt == 2, f"Expected trunk ceiling of 2 calls enforced, got {cnt}"

    for mc in mock_calls:
        ACTIVE_SIP_CALLS.pop(mc, None)


@pytest.mark.asyncio
async def test_concurrent_workers_atomic_lead_reservation(setup_stale_test_ctx, monkeypatch):
    """
    Proves:
    4. Multiple workers cannot reserve the same lead.
    5. Concurrent workers cannot exceed effective limit.
    """
    ctx = setup_stale_test_ctx
    biz_id = ctx["business_id"]
    agent = ctx["agent"]

    mock_calls = []
    async def mock_initiate(db, lead, ag, campaign=None, campaign_id=None, force_mock=False):
        await asyncio.sleep(0.01) # Yield to encourage interleaving
        cid = f"call-race-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=cid,
            business_id=biz_id,
            campaign_id=campaign_id,
            lead_id=lead.id,
            agent_id=ag.id if ag else None,
            status="RINGING",
            call_sid=f"sid-{cid}",
            direction="OUTBOUND",
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()
        ACTIVE_SIP_CALLS[cid] = {"call_active": [True], "sock_closed": False, "call_cid": cid, "db_call_id": cid}
        mock_calls.append(cid)
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            business_id=biz_id,
            name="Atomic Race Test",
            agent_id=agent.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio="2", # Capacity 2
        )
        db.add(camp)
        await db.commit()
        await db.refresh(camp)

        for i in range(5):
            ld = Lead(business_id=biz_id, first_name=f"LRace_{i}", phone=f"+91987655300{i}", status="NEW")
            db.add(ld)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=ld.id, status="PENDING"))
        await db.commit()

        # Run two workers concurrently for the same campaign
        await asyncio.gather(
            run_predictive_dialer_async(camp.id),
            run_predictive_dialer_async(camp.id),
        )

        cnt, _ = await count_campaign_active_calls(db, camp.id)
        assert cnt <= 2, f"Expected concurrency limit 2 never exceeded under concurrent workers, got {cnt}"

    for mc in mock_calls:
        ACTIVE_SIP_CALLS.pop(mc, None)
