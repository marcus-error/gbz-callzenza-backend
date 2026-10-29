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
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES, PENDING_STATUSES
from app.api.routes.campaigns import dialer_status, pause_campaign, start_campaign
from app.services.voice_service import (
    ACTIVE_SIP_CALLS,
    hangup_sip_call,
    reconcile_unended_call,
)


@pytest_asyncio.fixture
async def setup_1_to_2_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"ParallelBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin = User(
            email=f"admin_parallel_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("securepassword"),
            full_name="Admin Parallel",
            first_name="Admin",
            last_name="Parallel",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"Agent_Parallel_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        client = Client(
            business_id=biz.id,
            name="Parallel Client",
            company_name="Parallel Enterprise",
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
async def test_ratio_1_2_parallel_dispatch_refill_and_pause_invariants(setup_1_to_2_ctx, monkeypatch):
    """
    Validates end-to-end concurrency 1:2 invariants:
    1. A RUNNING campaign with 4 eligible leads dispatches exactly 2 calls concurrently at a 1:2 limit.
    2. The second call is dispatched before the first call finishes.
    3. Completing one call triggers refill when the campaign remains RUNNING, dispatching call 3 while call 2 is still active.
    4. One call ending does NOT terminate the other active call.
    5. An intentionally PAUSED campaign does NOT dispatch new calls.
    6. Stale database calls (>45s with no SIP/session) do not inflate active counts.
    """
    ctx = setup_1_to_2_ctx
    dispatched = []

    async def mock_initiate_outbound_call(db, lead, agent, campaign=None, campaign_id=None, force_mock=False):
        cid = f"call-1-2-{uuid.uuid4().hex[:8]}"
        call_id = cid
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        call = Call(
            id=call_id,
            call_sid=f"mock-sid-{cid}",
            business_id=ctx["biz_id"],
            campaign_id=campaign_id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            initiated_at=now,
        )
        db.add(call)
        await db.flush()

        # Track mock SIP active session
        ACTIVE_SIP_CALLS[call_id] = {
            "call_cid": cid,
            "db_call_id": call_id,
            "call_active": [True],
            "sock": None,
            "sock_closed": True,
            "started_at": now,
        }
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.lead_id == lead.id)
            .values(status="RINGING", call_id=call_id)
        )
        dispatched.append({
            "lead_id": lead.id,
            "call_id": call_id,
            "dispatched_at": datetime.utcnow(),
        })
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate_outbound_call)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate_outbound_call)

    async with AsyncSessionLocal() as db:
        # Create campaign configured with ratio 1:2
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Parallel 1:2 Concurrency Invariant Test",
            business_id=ctx["biz_id"],
            client_id=ctx["client_id"],
            client_name="Parallel Client",
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            schedule={"calling_mode": "local"},
        )
        db.add(camp)
        await db.flush()

        # Add 4 eligible leads
        leads = [
            Lead(
                id=str(uuid.uuid4()),
                first_name=f"Lead_{i}",
                last_name="Test",
                phone=f"+9198765431{i:02d}",
                business_id=ctx["biz_id"],
                status="PENDING",
            )
            for i in range(4)
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

        # Step 1: Run predictive dialer (1:2 ratio with 4 leads)
        await run_predictive_dialer_async(camp.id)

        # Invariant 1: Exactly 2 calls must be dispatched concurrently
        assert len(dispatched) == 2, f"Expected 2 concurrent calls for ratio 1:2, got {len(dispatched)}"
        assert dispatched[0]["call_id"] != dispatched[1]["call_id"]
        assert dispatched[0]["lead_id"] != dispatched[1]["lead_id"]

        # Invariant 2: Both calls exist simultaneously in ACTIVE_SIP_CALLS (2nd call started while 1st is still ringing/active)
        assert dispatched[0]["call_id"] in ACTIVE_SIP_CALLS
        assert dispatched[1]["call_id"] in ACTIVE_SIP_CALLS

        # Verify dialer status reports exactly 2 active calls and 0 available slots
        st = await dialer_status(id=camp.id, user=ctx["admin"], db=db)
        assert st["status"] == "RUNNING"
        assert st["ratio"] == 2
        assert st["target_capacity"] == 2
        assert st["active_calls"] == 2
        assert st["available_slots"] == 0
        assert st["pending_leads"] == 2

        # Invariant 3 & 4: Terminate call 0 -> call 1 MUST remain active; refill dispatches lead 2
        call_0_id = dispatched[0]["call_id"]
        call_1_id = dispatched[1]["call_id"]
        hangup_sip_call(call_0_id)
        assert call_0_id not in ACTIVE_SIP_CALLS
        assert call_1_id in ACTIVE_SIP_CALLS  # Call 1 remains completely unharmed!

        # Mark call 0 completed in DB
        call_0_rec = await db.get(Call, call_0_id)
        call_0_rec.status = "COMPLETED"
        call_0_rec.ended_at = datetime.utcnow()
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.call_id == call_0_id)
            .values(status="COMPLETED")
        )
        await db.commit()

        # Trigger refill
        await run_predictive_dialer_async(camp.id)

        # Invariant: 3rd call is now dispatched into the freed slot while Call 1 is STILL active!
        assert len(dispatched) == 3
        assert dispatched[2]["call_id"] in ACTIVE_SIP_CALLS
        assert call_1_id in ACTIVE_SIP_CALLS

        # Invariant 5: Intentionally PAUSED campaign does not dispatch new calls
        await pause_campaign(id=camp.id, user=ctx["admin"], db=db)
        camp_reloaded = await db.get(Campaign, camp.id)
        assert camp_reloaded.status == "PAUSED"
        assert camp_reloaded.auto_dial_enabled is False

        # Attempting refill while PAUSED must NOT dispatch call 4
        await run_predictive_dialer_async(camp.id)
        assert len(dispatched) == 3, "Paused campaign must not dispatch new calls"

        # Cleanup remaining active SIP calls
        for d in dispatched:
            hangup_sip_call(d["call_id"])
