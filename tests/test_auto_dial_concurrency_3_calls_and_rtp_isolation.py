import asyncio
import os
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
from sqlalchemy import select, func

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
from app.api.routes.campaigns import dialer_status
from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_leads
from app.services.voice_service import (
    allocate_unique_rtp_socket,
    ACTIVE_SIP_CALLS,
    hangup_sip_call,
)


@pytest_asyncio.fixture
async def setup_concurrency_regression_ctx():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"ConcurrencyBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin = User(
            email=f"admin_conc_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("securepassword"),
            full_name="Admin Concurrency",
            first_name="Admin",
            last_name="Concurrency",
            role="ADMIN",
            business_id=biz.id,
        )
        agent = AIAgent(
            name=f"Agent_Conc_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        client = Client(
            business_id=biz.id,
            name="Concurrency Client",
            company_name="Concurrency Enterprise",
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


def test_rtp_unique_port_allocator_isolation():
    """
    Test 1: RTP unique port allocator must allocate distinct, exclusive UDP sockets.
    Concurrent sockets must not collide or overwrite each other.
    """
    sockets = []
    ports = []
    try:
        for _ in range(5):
            sock, port = allocate_unique_rtp_socket()
            assert sock is not None
            assert port > 0
            sockets.append(sock)
            ports.append(port)

        # All 5 ports must be strictly unique
        assert len(set(ports)) == 5
        # Verify socket handles are distinct
        assert len(set(s.fileno() for s in sockets)) == 5
    finally:
        for s in sockets:
            try:
                s.close()
            except Exception:
                pass


@pytest.mark.asyncio
async def test_three_simultaneous_call_dispatches_with_sip_isolation(setup_concurrency_regression_ctx, monkeypatch):
    """
    Test 2: Auto-dial ratio 1:3 must dispatch 3 distinct calls concurrently.
    All 3 calls must maintain distinct call/session IDs, isolated RTP allocations,
    and coexist in ACTIVE_SIP_CALLS without call 2 or 3 killing call 1.
    """
    ctx = setup_concurrency_regression_ctx
    dispatched = []

    # Mock real-time provider dispatch that verifies non-destructive ACTIVE_SIP_CALLS registration
    async def mock_isolated_sip_initiate(db, lead, agent=None, *args, **kwargs):
        call_id = f"call-sip-{uuid.uuid4().hex[:8]}"
        cid = f"call-{uuid.uuid4().hex[:8]}@callzenza"

        sock, rtp_port = allocate_unique_rtp_socket()

        # Simulate ACTIVE_SIP_CALLS registration per call
        call_info_record = {
            "sock": sock,
            "rtp_sock": sock,
            "call_active": [True],
            "target_to": lead.phone,
            "sip_host": "127.0.0.1",
            "sip_port": 5060,
            "contact_ip": "127.0.0.1",
            "listen_port": rtp_port,
            "sip_user": "test_user",
            "from_tag": f"tag-{uuid.uuid4().hex[:8]}",
            "call_cid": cid,
            "db_call_id": call_id,
            "hangup_started": False,
            "sock_closed": False,
            "answered": False,
        }
        ACTIVE_SIP_CALLS[call_id] = call_info_record
        ACTIVE_SIP_CALLS[cid] = call_info_record

        call = Call(
            id=call_id,
            business_id=lead.business_id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+1800555000",
            to_number=lead.phone,
            call_sid=cid,
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
        dispatched.append({"lead_id": lead.id, "call_id": call_id, "cid": cid, "rtp_port": rtp_port})
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_isolated_sip_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_isolated_sip_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Auto-Dial 1:3 Concurrency Regression",
            business_id=ctx["biz_id"],
            client_id=ctx["client_id"],
            client_name="Concurrency Client",
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
                last_name="Test",
                phone=f"+9198765430{i:02d}",
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

        # Step 1: Run predictive dialer (1:3 ratio with 5 leads)
        await run_predictive_dialer_async(camp.id)

        # Step 2: Verify exactly 3 distinct concurrent outbound calls
        assert len(dispatched) == 3, f"Expected 3 calls, got {len(dispatched)}"
        distinct_leads = set(d["lead_id"] for d in dispatched)
        assert len(distinct_leads) == 3
        distinct_call_ids = set(d["call_id"] for d in dispatched)
        assert len(distinct_call_ids) == 3
        distinct_ports = set(d["rtp_port"] for d in dispatched)
        assert len(distinct_ports) == 3

        # Step 3: Verify all 3 calls coexist simultaneously in ACTIVE_SIP_CALLS
        for d in dispatched:
            assert d["call_id"] in ACTIVE_SIP_CALLS
            assert d["cid"] in ACTIVE_SIP_CALLS
            assert ACTIVE_SIP_CALLS[d["call_id"]]["call_active"][0] is True

        # Step 4: Verify accurate concurrency status via dialer_status API
        status_res = await dialer_status(id=camp.id, user=ctx["admin"], db=db)
        assert status_res["status"] == "RUNNING"
        assert status_res["ratio"] == 3
        assert status_res["target_capacity"] == 3
        assert status_res["active_calls"] == 3
        assert status_res["available_slots"] == 0
        assert status_res["pending_leads"] == 2

        # Step 5: Verify AI Agent leads API returns all 3 active calls
        leads_res = await get_campaign_ai_agent_leads(
            campaign_id=camp.id,
            user=ctx["admin"],
            db=db,
            limit=100,
        )
        leads_items = leads_res["leads"] if isinstance(leads_res, dict) else getattr(leads_res, "leads", [])
        active_in_api = [
            l for l in leads_items
            if (l.get("call_status") if isinstance(l, dict) else getattr(l, "call_status", "")) in ("DIALING", "RINGING", "CONNECTED")
        ]
        assert len(active_in_api) == 3

        # Step 6: Simulate call 1 ending -> Slot frees up -> Refill initiates call 4
        call_to_end = dispatched[0]
        # Hangup call 1 using hangup_sip_call
        hangup_sip_call(call_to_end["call_id"])
        assert call_to_end["call_id"] not in ACTIVE_SIP_CALLS

        # Calls 2 and 3 must STILL be active in ACTIVE_SIP_CALLS!
        assert dispatched[1]["call_id"] in ACTIVE_SIP_CALLS
        assert dispatched[2]["call_id"] in ACTIVE_SIP_CALLS
        assert ACTIVE_SIP_CALLS[dispatched[1]["call_id"]]["call_active"][0] is True
        assert ACTIVE_SIP_CALLS[dispatched[2]["call_id"]]["call_active"][0] is True

        # Update DB for finished call
        from sqlalchemy import update
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.call_id == call_to_end["call_id"])
            .values(status="COMPLETED")
        )
        await db.execute(
            update(Call)
            .where(Call.id == call_to_end["call_id"])
            .values(status="COMPLETED", ended_at=datetime.utcnow())
        )
        await db.commit()

        # Step 7: Trigger dialer refill
        await run_predictive_dialer_async(camp.id)

        # Total dispatched calls must now be 4 (calls 2, 3 and newly refilled call 4 are active)
        assert len(dispatched) == 4
        assert len(set(d["lead_id"] for d in dispatched)) == 4

        # Step 8: Verify concurrency counter is back at 3/3
        status_after_refill = await dialer_status(id=camp.id, user=ctx["admin"], db=db)
        assert status_after_refill["status"] == "RUNNING"
        assert status_after_refill["active_calls"] == 3
        assert status_after_refill["available_slots"] == 0
        assert status_after_refill["pending_leads"] == 1

        # Clean up remaining mocked socket handles
        for d in dispatched:
            hangup_sip_call(d["call_id"])
