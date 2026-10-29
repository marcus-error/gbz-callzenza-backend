import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
from sqlalchemy import select, func, update

from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, AIAgent, Call, SIPTrunk
from app.core.security import hash_password
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES, _parse_ratio
from app.workers.tasks import trigger_campaign_refill
from app.api.routes.campaigns import dialer_status


@pytest_asyncio.fixture
async def dynamic_ratio_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"DynamicRatioBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
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
            name=f"DynamicAgent_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-anusha-1"
        )
        db.add_all([admin_user, agent])
        await db.commit()

        yield {
            "biz_id": biz.id,
            "admin": admin_user,
            "agent_id": agent.id,
        }


# ============================================================================
# 1. Parameterized Ratio Parsing & Normalization Tests
# ============================================================================
@pytest.mark.parametrize("input_ratio,expected_val", [
    ("1:1", 1),
    ("1:2", 2),
    ("1:3", 3),
    ("1:5", 5),
    ("1:6", 6),
    ("1:10", 10),
    ("2:1", 2),
    ("6:1", 6),
    ("10:1", 10),
    (1, 1),
    (6, 6),
    (10, 10),
    ("6", 6),
    (None, 1),
    ("invalid", 1),
    (0, 1),
    (-5, 1),
])
def test_all_supported_ratio_formats_parsed_correctly(input_ratio, expected_val):
    """Verify that _parse_ratio and CampaignCreate handle all ratio formats dynamically."""
    # Test _parse_ratio helper
    assert _parse_ratio(input_ratio) == expected_val

    # Test Pydantic CampaignCreate validation
    camp = CampaignCreate(name="Dynamic Test", auto_dial_ratio=input_ratio)
    assert camp.auto_dial_ratio == expected_val


# ============================================================================
# 2. Parameterized Concurrent Outbound Dispatch Tests (1:1, 1:2, 1:3, 1:5, 1:6, 1:10)
# ============================================================================
@pytest.mark.parametrize("configured_ratio,expected_concurrency", [
    (1, 1),
    (2, 2),
    (3, 3),
    (5, 5),
    (6, 6),
    (10, 10),
])
@pytest.mark.asyncio
async def test_dynamic_ratios_dispatch_expected_concurrency(dynamic_ratio_ctx, configured_ratio, expected_concurrency, monkeypatch):
    """
    Verify that a campaign configured with any ratio R dispatches min(R, eligible_leads)
    concurrent calls simultaneously without waiting for prior calls to complete.
    """
    biz_id = dynamic_ratio_ctx["biz_id"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    dispatched_calls = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        campaign = kwargs.get("campaign")
        dispatched_calls.append({
            "lead_id": lead.id,
            "phone": lead.phone,
            "campaign_id": campaign.id if campaign else None,
        })
        call = Call(
            id=str(uuid.uuid4()),
            lead_id=lead.id,
            campaign_id=campaign.id if campaign else None,
            business_id=biz_id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+15550001",
            to_number=lead.phone or "+15550002",
        )
        db.add(call)
        await db.flush()
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name=f"Dynamic_Ratio_{configured_ratio}_{uuid.uuid4().hex[:6]}",
            business_id=biz_id,
            agent_id=agent_id,
            agent_type="AI_VOICE",
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=configured_ratio,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        # Seed 15 distinct eligible leads
        for i in range(15):
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="Test",
                phone=f"+1555{100000 + i}",
                status="PENDING",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(
                campaign_id=camp.id,
                lead_id=lead.id,
                status="PENDING",
            ))
        await db.commit()

        # Execute predictive dialer
        await run_predictive_dialer_async(camp.id)

        # Verify dispatched count matches expected ratio
        assert len(dispatched_calls) == expected_concurrency

        # Verify all dispatched lead IDs are unique
        dispatched_lead_ids = [d["lead_id"] for d in dispatched_calls]
        assert len(set(dispatched_lead_ids)) == expected_concurrency

        # Verify CampaignLead statuses are updated to RINGING
        stmt = select(CampaignLead.status).where(
            CampaignLead.campaign_id == camp.id,
            CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
        )
        active_leads = (await db.scalars(stmt)).all()
        assert len(active_leads) == expected_concurrency


# ============================================================================
# 3. Provider Trunk Limit Constraint Test
# ============================================================================
@pytest.mark.asyncio
async def test_provider_trunk_capacity_limits_dispatch(dynamic_ratio_ctx, monkeypatch):
    """
    Even if ratio is 1:6, if the assigned SIP trunk has max_channels = 2,
    only 2 calls should be dispatched concurrently.
    """
    biz_id = dynamic_ratio_ctx["biz_id"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    dispatched = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        dispatched.append(lead.id)
        call = Call(
            id=str(uuid.uuid4()),
            lead_id=lead.id,
            campaign_id=kwargs.get("campaign_id") or (kwargs.get("campaign").id if kwargs.get("campaign") else None),
            business_id=biz_id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+15550001",
            to_number=lead.phone,
        )
        db.add(call)
        await db.flush()
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        from app.db.models import SIPCarrier
        carrier = SIPCarrier(
            id=str(uuid.uuid4()),
            business_id=biz_id,
            name="Test Carrier",
            sip_host="127.0.0.1",
        )
        db.add(carrier)
        await db.flush()

        trunk = SIPTrunk(
            id=str(uuid.uuid4()),
            business_id=biz_id,
            carrier_id=carrier.id,
            name="Limited Trunk",
            max_concurrency=2,  # Throttled to 2 channels!
        )
        db.add(trunk)
        await db.flush()

        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Throttled Trunk Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            trunk_id=trunk.id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=6,  # Configured as 1:6!
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        for i in range(10):
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="Trunk",
                phone=f"+155599{i}",
                status="PENDING",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING"))
        await db.commit()

        await run_predictive_dialer_async(camp.id)

        # Must be throttled to trunk capacity of 2, not 6
        assert len(dispatched) == 2


# ============================================================================
# 4. Atomic Lead Reservation & Concurrent Worker Safety Test
# ============================================================================
@pytest.mark.asyncio
async def test_concurrent_dialer_runs_do_not_double_reserve_leads(dynamic_ratio_ctx, monkeypatch):
    """
    When multiple dialer tasks trigger simultaneously for the same campaign,
    no lead is dialed or reserved more than once.
    """
    biz_id = dynamic_ratio_ctx["biz_id"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    all_dialed = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        all_dialed.append(lead.id)
        call = Call(
            id=str(uuid.uuid4()),
            lead_id=lead.id,
            campaign_id=kwargs.get("campaign_id") or (kwargs.get("campaign").id if kwargs.get("campaign") else None),
            business_id=biz_id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+15550001",
            to_number=lead.phone,
        )
        db.add(call)
        await db.flush()
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Race Safety Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=6,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        for i in range(12):
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="Race",
                phone=f"+155588{i:02d}",
                status="PENDING",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING"))
        await db.commit()

        # Run 3 concurrent dialer workers simultaneously
        await asyncio.gather(
            run_predictive_dialer_async(camp.id),
            run_predictive_dialer_async(camp.id),
            run_predictive_dialer_async(camp.id),
        )

        # No duplicate lead IDs dialed
        assert len(all_dialed) == len(set(all_dialed))
        # Concurrency must not exceed ratio of 6
        assert len(all_dialed) <= 6


# ============================================================================
# 5. Dialer Status Endpoint Live Concurrency Accuracy Test
# ============================================================================
@pytest.mark.asyncio
async def test_dialer_status_endpoint_accuracy(dynamic_ratio_ctx):
    """
    Verify that the dialer_status endpoint returns live details:
    ratio, target_capacity, active_calls, available_slots, pending_leads, ringing_calls, completed_calls.
    """
    biz_id = dynamic_ratio_ctx["biz_id"]
    admin = dynamic_ratio_ctx["admin"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Status Check Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=6,
        )
        db.add(camp)
        await db.flush()

        # Add 3 RINGING, 2 COMPLETED, 1 FAILED, 4 PENDING leads
        statuses = ["RINGING", "RINGING", "RINGING", "COMPLETED", "COMPLETED", "FAILED", "PENDING", "PENDING", "PENDING", "PENDING"]
        for i, st in enumerate(statuses):
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="Status",
                phone=f"+155577{i:02d}",
                status="PENDING",
            )
            db.add(lead)
            await db.flush()
            db.add(CampaignLead(campaign_id=camp.id, lead_id=lead.id, status=st))
        await db.commit()

        status_result = await dialer_status(camp.id, user=admin, db=db)

        assert status_result["ratio"] == 6
        assert status_result["target_capacity"] == 6
        assert status_result["active_calls"] == 3
        assert status_result["available_slots"] == 3  # 6 - 3 = 3 open slots
        assert status_result["pending_leads"] == 4
        assert status_result["ringing_calls"] == 3
        assert status_result["completed_calls"] == 2
        assert status_result["failed_calls"] == 1


# ============================================================================
# 6. End-to-End Start Campaign Route -> Concurrent Dialing & Refill
# ============================================================================
@pytest.mark.asyncio
async def test_start_campaign_triggers_concurrent_dialing_and_refill(dynamic_ratio_ctx, monkeypatch):
    """
    Verify that calling start_campaign with auto_dial_ratio=6:
    1. Successfully starts the campaign and updates status to RUNNING.
    2. Exactly 6 distinct calls are concurrently initiated.
    3. Status endpoint returns active_calls=6, available_slots=0, pending_leads=6.
    4. Completing 2 calls frees capacity and triggers refill of 2 more calls.
    """
    from fastapi import BackgroundTasks
    from app.api.routes.campaigns import start_campaign
    from app.workers.dialer import run_predictive_dialer_async
    from app.db.models import Client

    biz_id = dynamic_ratio_ctx["biz_id"]
    admin = dynamic_ratio_ctx["admin"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    dispatched = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        dispatched.append(lead.id)
        call = Call(
            id=str(uuid.uuid4()),
            lead_id=lead.id,
            campaign_id=kwargs.get("campaign_id") or (kwargs.get("campaign").id if kwargs.get("campaign") else None),
            business_id=biz_id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+15550001",
            to_number=lead.phone,
        )
        db.add(call)
        await db.flush()
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        # Create client
        client = Client(id=str(uuid.uuid4()), business_id=biz_id, name="Test Client")
        db.add(client)
        await db.flush()

        # Create campaign with 1:6 ratio
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="E2E Ratio 1:6 Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            client_id=client.id,
            client_name="Test Client",
            status="IDLE",
            auto_dial_enabled=True,
            auto_dial_ratio=6,
            schedule={"calling_mode": "local"}
        )
        db.add(camp)
        await db.flush()

        # 12 leads
        for i in range(12):
            lead = Lead(
                id=str(uuid.uuid4()),
                business_id=biz_id,
                first_name=f"Lead_{i}",
                last_name="E2E",
                phone=f"+155599{i:02d}",
                status="PENDING",
            )
            db.add(lead)
            await db.flush()
            cl = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(cl)
        await db.commit()

        # Call start_campaign route (which triggers campaign refill in the background)
        bg = BackgroundTasks()
        started_camp = await start_campaign(camp.id, background_tasks=bg, user=admin, db=db)
        assert started_camp.status == "RUNNING"

        # Wait for background dialer task scheduled by start_campaign to finish dispatch
        for _ in range(50):
            await db.commit()
            ringing_cnt = (await db.scalar(
                select(func.count(CampaignLead.id)).where(
                    CampaignLead.campaign_id == camp.id,
                    CampaignLead.status == "RINGING"
                )
            )) or 0
            if len(dispatched) >= 6 and ringing_cnt >= 6:
                break
            await asyncio.sleep(0.1)

        # Verify exactly 6 concurrent calls dispatched
        assert len(dispatched) == 6
        assert len(set(dispatched)) == 6

        # Check dialer status endpoint
        st = await dialer_status(camp.id, user=admin, db=db)
        assert st["ratio"] == 6
        assert st["target_capacity"] == 6
        assert st["active_calls"] == 6
        assert st["available_slots"] == 0
        assert st["pending_leads"] == 6

        # Simulate 2 calls completing
        active_leads = (await db.scalars(
            select(CampaignLead).where(CampaignLead.campaign_id == camp.id, CampaignLead.status == "RINGING")
        )).all()
        assert len(active_leads) == 6

        for cl_done in active_leads[:2]:
            cl_done.status = "COMPLETED"
            if cl_done.call_id:
                call_obj = await db.get(Call, cl_done.call_id)
                if call_obj:
                    call_obj.status = "COMPLETED"
                    call_obj.ended_at = datetime.utcnow()
                    db.add(call_obj)
        await db.commit()

        # Refill dispatches 2 more calls into open capacity
        await run_predictive_dialer_async(camp.id)

        for _ in range(40):
            if len(dispatched) >= 8:
                break
            await asyncio.sleep(0.05)

        # 2 more leads dispatched, bringing total dispatched to 8!
        assert len(dispatched) == 8
        assert len(set(dispatched)) == 8

        # Status check after refill
        st2 = await dialer_status(camp.id, user=admin, db=db)
        assert st2["active_calls"] == 6
        assert st2["available_slots"] == 0
        assert st2["pending_leads"] == 4
        assert st2["completed_calls"] == 2


# ============================================================================
# 7. Persistence & Admin Campaign List Sync Test (1:3 -> 1:6 -> 1:1)
# ============================================================================
@pytest.mark.asyncio
async def test_auto_dial_ratio_persistence_and_admin_list_sync(dynamic_ratio_ctx):
    """
    Verify:
    1. Creating a campaign with 1:3 stores 3 in DB.
    2. list_campaigns (/api/campaigns) returns auto_dial_ratio = 3.
    3. admin_list_campaigns (/api/admin/campaigns) returns auto_dial_ratio = 3.
    4. get_campaign_ai_agent_dashboard returns campaign.auto_dial_ratio = 3.
    5. Editing campaign without changing ratio retains 3.
    6. Updating ratio to 1:6 reflects as 6 across all endpoints.
    7. Ratio 1:1 works consistently.
    """
    from app.schemas.campaign import CampaignCreate, CampaignUpdate
    from app.api.routes.campaigns import create_campaign, update_campaign, list_campaigns
    from app.api.routes.admin_campaigns import admin_list_campaigns
    from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_dashboard
    from app.db.models import Client

    biz_id = dynamic_ratio_ctx["biz_id"]
    admin = dynamic_ratio_ctx["admin"]
    agent_id = dynamic_ratio_ctx["agent_id"]

    async with AsyncSessionLocal() as db:
        # Create client
        client = Client(id=str(uuid.uuid4()), business_id=biz_id, name="Sync Test Client")
        db.add(client)
        await db.flush()

        # 1. Create campaign with auto_dial_ratio = 3 (representing 1:3)
        create_payload = CampaignCreate(
            name="1:3 Sync Test Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            client_id=client.id,
            client_name="Sync Test Client",
            auto_dial_enabled=True,
            auto_dial_ratio="1:3",
            lead_ids=[]
        )
        created_camp = await create_campaign(create_payload, user=admin, db=db)
        assert created_camp.auto_dial_ratio == 3

        # 2. Verify DB persistence directly
        camp_in_db = await db.get(Campaign, created_camp.id)
        assert camp_in_db.auto_dial_ratio == 3

        # 3. Verify user list_campaigns returns 3
        user_camps = await list_campaigns(user=admin, db=db)
        matched_user_camp = next((c for c in user_camps if c.id == created_camp.id), None)
        assert matched_user_camp is not None
        assert matched_user_camp.auto_dial_ratio == 3

        # 4. Verify admin_list_campaigns (/api/admin/campaigns) returns 3
        admin_camps = await admin_list_campaigns(db=db, current_user=admin)
        matched_admin_camp = next((c for c in admin_camps if c["id"] == created_camp.id), None)
        assert matched_admin_camp is not None
        assert matched_admin_camp["auto_dial_ratio"] == 3

        # 5. Verify get_campaign_ai_agent_dashboard returns 3
        dash = await get_campaign_ai_agent_dashboard(created_camp.id, user=admin, db=db)
        assert dash["campaign"]["auto_dial_ratio"] == 3

        # 6. Edit campaign without changing ratio -> remains 3
        update_payload_no_ratio = CampaignUpdate(name="Renamed 1:3 Campaign")
        updated_camp = await update_campaign(created_camp.id, payload=update_payload_no_ratio, user=admin, db=db)
        assert updated_camp.auto_dial_ratio == 3

        # Re-check admin list
        admin_camps2 = await admin_list_campaigns(db=db, current_user=admin)
        matched2 = next((c for c in admin_camps2 if c["id"] == created_camp.id), None)
        assert matched2["auto_dial_ratio"] == 3

        # 7. Update ratio to 1:6
        update_payload_ratio_6 = CampaignUpdate(auto_dial_ratio="1:6")
        updated_camp6 = await update_campaign(created_camp.id, payload=update_payload_ratio_6, user=admin, db=db)
        assert updated_camp6.auto_dial_ratio == 6

        # Check DB, user list, admin list, and dashboard
        await db.refresh(camp_in_db)
        assert camp_in_db.auto_dial_ratio == 6

        admin_camps3 = await admin_list_campaigns(db=db, current_user=admin)
        matched3 = next((c for c in admin_camps3 if c["id"] == created_camp.id), None)
        assert matched3["auto_dial_ratio"] == 6

        dash3 = await get_campaign_ai_agent_dashboard(created_camp.id, user=admin, db=db)
        assert dash3["campaign"]["auto_dial_ratio"] == 6

        # 8. Create campaign with 1:1 -> returns 1
        create_payload_1 = CampaignCreate(
            name="1:1 Sync Test Campaign",
            business_id=biz_id,
            agent_id=agent_id,
            client_id=client.id,
            client_name="Sync Test Client",
            auto_dial_enabled=True,
            auto_dial_ratio="1:1",
            lead_ids=[]
        )
        created_camp_1 = await create_campaign(create_payload_1, user=admin, db=db)
        assert created_camp_1.auto_dial_ratio == 1

        admin_camps4 = await admin_list_campaigns(db=db, current_user=admin)
        matched4 = next((c for c in admin_camps4 if c["id"] == created_camp_1.id), None)
        assert matched4["auto_dial_ratio"] == 1


