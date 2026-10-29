import asyncio
import base64
import json
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from sqlalchemy import select, func

from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, CampaignLead, AIAgent, Call, AuditLog
from app.core.security import create_access_token, hash_password
from app.schemas.campaign import CampaignCreate, CampaignUpdate
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES
from app.workers.tasks import trigger_campaign_refill, _run_campaign_calls_async
from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    disconnect_browser_bargers,
    AUTHORIZED_SUPERVISOR_ROLES,
    _validate_active_call_for_monitoring
)
from app.services.sip_ai_bot import pause_sip_ai_bot, resume_sip_ai_bot
from app.services.voice_service import finalize_call_and_refill


@pytest_asyncio.fixture
async def setup_test_campaign():
    async with AsyncSessionLocal() as db:
        biz = Business(name=f"TestBiz_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        admin_user = User(
            email=f"admin_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Admin User",
            role="ADMIN",
            business_id=biz.id,
        )
        supervisor_user = User(
            email=f"sup_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Supervisor User",
            role="SUPERVISOR",
            business_id=biz.id,
        )
        viewer_user = User(
            email=f"viewer_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Viewer User",
            role="VIEWER",
            business_id=biz.id,
        )
        db.add_all([admin_user, supervisor_user, viewer_user])
        await db.flush()

        agent = AIAgent(
            name=f"TestAgent_{uuid.uuid4().hex[:6]}",
            business_id=biz.id,
            status="ACTIVE",
            voice_id="snapserve-en-1"
        )
        db.add(agent)
        await db.commit()

        biz_id = biz.id
        admin_id = admin_user.id
        supervisor_id = supervisor_user.id
        viewer_id = viewer_user.id
        agent_id = agent.id

    yield {
        "biz_id": biz_id,
        "admin_id": admin_id,
        "supervisor_id": supervisor_id,
        "viewer_id": viewer_id,
        "agent_id": agent_id,
    }


@pytest.mark.asyncio
async def test_ratio_schema_normalization():
    """Phase 2: Verify auto_dial_ratio accepts both int and '1:X' string format."""
    # Test CampaignCreate
    c1 = CampaignCreate(name="Camp 1", auto_dial_ratio=1)
    assert c1.auto_dial_ratio == 1

    c2 = CampaignCreate(name="Camp 2", auto_dial_ratio="1:2")
    assert c2.auto_dial_ratio == 2

    c3 = CampaignCreate(name="Camp 3", auto_dial_ratio="1:3")
    assert c3.auto_dial_ratio == 3

    c5 = CampaignCreate(name="Camp 5", auto_dial_ratio=" 1:5 ")
    assert c5.auto_dial_ratio == 5

    # Test CampaignUpdate
    u1 = CampaignUpdate(auto_dial_ratio="1:3")
    assert u1.auto_dial_ratio == 3


@pytest.mark.asyncio
async def test_ratio_1_to_1_capacity_limit(setup_test_campaign):
    """Phase 2: Ratio 1:1 dispatches at most 1 active call simultaneously."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Ratio 1:1 Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        for i in range(5):
            lead = Lead(first_name=f"Lead {i}", phone=f"+100000000{i}", business_id=ctx["biz_id"])
            db.add(lead)
            await db.flush()
            clead = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(clead)
        await db.commit()

        # Run predictive dialer
        await run_predictive_dialer_async(camp.id)

        # Verify active calls count is exactly 1 (ratio = 1)
        stmt_active = select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == camp.id,
            CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
        )
        active_count = await db.scalar(stmt_active)
        assert active_count == 1, f"Expected 1 active call for ratio 1:1, got {active_count}"

        # Second dialer run must respect full capacity and not dial additional leads
        await run_predictive_dialer_async(camp.id)
        active_count_2 = await db.scalar(stmt_active)
        assert active_count_2 == 1, f"Dialer exceeded capacity on second tick: {active_count_2}"


@pytest.mark.asyncio
async def test_ratio_1_to_2_capacity_limit(setup_test_campaign):
    """Phase 2: Ratio 1:2 dispatches at most 2 active calls simultaneously."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Ratio 1:2 Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=2,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        for i in range(5):
            lead = Lead(first_name=f"Lead {i}", phone=f"+100000001{i}", business_id=ctx["biz_id"])
            db.add(lead)
            await db.flush()
            clead = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(clead)
        await db.commit()

        await run_predictive_dialer_async(camp.id)

        stmt_active = select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == camp.id,
            CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
        )
        active_count = await db.scalar(stmt_active)
        assert active_count == 2, f"Expected 2 active calls for ratio 1:2, got {active_count}"


@pytest.mark.asyncio
async def test_ratio_1_to_3_capacity_limit(setup_test_campaign):
    """Phase 2: Ratio 1:3 dispatches at most 3 active calls simultaneously."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Ratio 1:3 Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=3,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        for i in range(6):
            lead = Lead(first_name=f"Lead {i}", phone=f"+100000002{i}", business_id=ctx["biz_id"])
            db.add(lead)
            await db.flush()
            clead = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(clead)
        await db.commit()

        await run_predictive_dialer_async(camp.id)

        stmt_active = select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == camp.id,
            CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
        )
        active_count = await db.scalar(stmt_active)
        assert active_count == 3, f"Expected 3 active calls for ratio 1:3, got {active_count}"


@pytest.mark.asyncio
async def test_calling_status_counts_toward_capacity(setup_test_campaign):
    """Phase 2: Calls in 'CALLING' status occupy capacity and prevent over-dialing."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Calling Status Test",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        # Lead 1 is currently in CALLING state (as set by Twilio in-progress webhook)
        lead1 = Lead(first_name="Lead 1", phone="+1111111111", business_id=ctx["biz_id"])
        db.add(lead1)
        await db.flush()
        clead1 = CampaignLead(campaign_id=camp.id, lead_id=lead1.id, status="CALLING")
        db.add(clead1)

        # Lead 2 is PENDING
        lead2 = Lead(first_name="Lead 2", phone="+1111111112", business_id=ctx["biz_id"])
        db.add(lead2)
        await db.flush()
        clead2 = CampaignLead(campaign_id=camp.id, lead_id=lead2.id, status="PENDING")
        db.add(clead2)
        await db.commit()

        # Run dialer
        await run_predictive_dialer_async(camp.id)

        # Lead 2 must remain PENDING because Lead 1 in CALLING state fills the 1:1 capacity
        await db.refresh(clead2)
        assert clead2.status == "PENDING", f"Expected Lead 2 to remain PENDING, got {clead2.status}"


@pytest.mark.asyncio
async def test_terminal_event_frees_capacity_and_triggers_refill(setup_test_campaign):
    """Phase 3: When an active call terminates, capacity is freed and automatically refilled."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Refill Test Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        lead1 = Lead(first_name="Lead 1", phone="+1222222221", business_id=ctx["biz_id"])
        lead2 = Lead(first_name="Lead 2", phone="+1222222222", business_id=ctx["biz_id"])
        db.add_all([lead1, lead2])
        await db.flush()

        clead1 = CampaignLead(campaign_id=camp.id, lead_id=lead1.id, status="PENDING")
        clead2 = CampaignLead(campaign_id=camp.id, lead_id=lead2.id, status="PENDING")
        db.add_all([clead1, clead2])
        await db.commit()

        # Initial dial: dispatches Lead 1
        await run_predictive_dialer_async(camp.id)
        await db.refresh(clead1)
        await db.refresh(clead2)
        active_lead = clead1 if clead1.status in ACTIVE_DIALER_STATUSES else clead2
        pending_lead = clead2 if active_lead is clead1 else clead1

        assert active_lead.status in ACTIVE_DIALER_STATUSES
        assert pending_lead.status == "PENDING"
        active_call_id = active_lead.call_id

        # Terminal event: finalize active call via finalize_call_and_refill
        await finalize_call_and_refill(active_call_id, "completed", 35)

        # Trigger refill (as done by terminal hook)
        await _run_campaign_calls_async(camp.id)

        await db.refresh(active_lead)
        await db.refresh(pending_lead)

        assert active_lead.status == "COMPLETED"
        assert pending_lead.status in ACTIVE_DIALER_STATUSES, f"Expected pending lead to be refilled into active status, got {pending_lead.status}"


@pytest.mark.asyncio
async def test_campaign_completion_invariant(setup_test_campaign):
    """Phase 2/3: Campaign remains RUNNING while active calls exist even if pending leads == 0."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Completion Invariant Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        lead = Lead(first_name="Solo Lead", phone="+1333333331", business_id=ctx["biz_id"])
        db.add(lead)
        await db.flush()

        clead = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
        db.add(clead)
        await db.commit()

        # Dial the only lead
        await run_predictive_dialer_async(camp.id)
        await db.refresh(camp)
        await db.refresh(clead)

        # Active call is ongoing, 0 pending leads remaining
        assert clead.status in ACTIVE_DIALER_STATUSES
        assert camp.status == "RUNNING", "Campaign must not be marked COMPLETED while call is active!"

        # Call completes
        await finalize_call_and_refill(clead.call_id, "completed", 20)

        # Run dialer completion check
        await run_predictive_dialer_async(camp.id)
        await db.refresh(camp)

        assert camp.status == "COMPLETED", f"Expected COMPLETED once all calls finished, got {camp.status}"


@pytest.mark.asyncio
async def test_concurrent_worker_capacity_limit_safeguard(setup_test_campaign):
    """Phase 4 & Safeguard 1: Concurrent worker race check reverts excess reservations."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            name="Concurrency Test Campaign",
            business_id=ctx["biz_id"],
            agent_id=ctx["agent_id"],
            status="RUNNING",
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local", "mock_active": True}
        )
        db.add(camp)
        await db.flush()

        for i in range(4):
            lead = Lead(first_name=f"Lead {i}", phone=f"+144444444{i}", business_id=ctx["biz_id"])
            db.add(lead)
            await db.flush()
            clead = CampaignLead(campaign_id=camp.id, lead_id=lead.id, status="PENDING")
            db.add(clead)
        await db.commit()

        # Simulate 2 workers executing concurrently
        await asyncio.gather(
            run_predictive_dialer_async(camp.id),
            run_predictive_dialer_async(camp.id)
        )

        # Capacity limit of 1 must never be exceeded
        stmt_active = select(func.count(CampaignLead.id)).where(
            CampaignLead.campaign_id == camp.id,
            CampaignLead.status.in_(ACTIVE_DIALER_STATUSES)
        )
        active_count = await db.scalar(stmt_active)
        assert active_count <= 1, f"Concurrency race violated capacity limit! Active count: {active_count}"


@pytest.mark.asyncio
async def test_deterministic_campaign_lead_matching(setup_test_campaign):
    """Phase 5: Lead in multiple campaigns matches exact campaign_id and call_id."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        # Same shared lead across two campaigns
        shared_lead = Lead(first_name="Shared Lead", phone="+1555555555", business_id=ctx["biz_id"])
        db.add(shared_lead)
        await db.flush()

        camp_a = Campaign(name="Camp A", business_id=ctx["biz_id"], status="RUNNING", auto_dial_ratio=1)
        camp_b = Campaign(name="Camp B", business_id=ctx["biz_id"], status="RUNNING", auto_dial_ratio=1)
        db.add_all([camp_a, camp_b])
        await db.flush()

        clead_a = CampaignLead(campaign_id=camp_a.id, lead_id=shared_lead.id, status="PENDING")
        clead_b = CampaignLead(campaign_id=camp_b.id, lead_id=shared_lead.id, status="PENDING")
        db.add_all([clead_a, clead_b])
        await db.commit()

        # Create a call specifically for Campaign A
        call_a = Call(
            campaign_id=camp_a.id,
            lead_id=shared_lead.id,
            business_id=ctx["biz_id"],
            direction="OUTBOUND",
            status="CONNECTED"
        )
        db.add(call_a)
        await db.flush()
        clead_a.call_id = call_a.id
        clead_a.status = "CONNECTED"
        await db.commit()

        # Finalize Call A
        await finalize_call_and_refill(call_a.id, "completed", 45)

        await db.refresh(clead_a)
        await db.refresh(clead_b)

        # Ensure only Campaign A's lead is completed, Campaign B remains PENDING
        assert clead_a.status == "COMPLETED"
        assert clead_b.status == "PENDING", f"Cross-campaign lead contamination! clead_b status: {clead_b.status}"


@pytest.mark.asyncio
async def test_barge_in_authorization(setup_test_campaign):
    """Phase 6 & 8: Verify unauthorized roles are rejected and valid supervisor/admin are accepted."""
    ctx = setup_test_campaign
    async with AsyncSessionLocal() as db:
        call = Call(
            business_id=ctx["biz_id"],
            direction="OUTBOUND",
            status="CONNECTED"
        )
        db.add(call)
        await db.commit()
        call_id = call.id

    # Valid active call validation
    call_obj, call_status = await _validate_active_call_for_monitoring(call_id)
    assert call_status == "ACTIVE"
    assert call_obj.id == call_id

    # Terminated call rejection
    async with AsyncSessionLocal() as db:
        call = await db.get(Call, call_id)
        call.status = "COMPLETED"
        await db.commit()

    _, terminated_status = await _validate_active_call_for_monitoring(call_id)
    assert terminated_status == "TERMINATED"

    # Role verification
    assert "SUPERVISOR" in AUTHORIZED_SUPERVISOR_ROLES
    assert "ADMIN" in AUTHORIZED_SUPERVISOR_ROLES
    assert "SUPER_ADMIN" in AUTHORIZED_SUPERVISOR_ROLES
    assert "VIEWER" not in AUTHORIZED_SUPERVISOR_ROLES
    assert "MEMBER" not in AUTHORIZED_SUPERVISOR_ROLES


@pytest.mark.asyncio
async def test_barge_in_per_call_isolation_and_ai_yielding(setup_test_campaign):
    """Phase 6 & Safeguard 2: Audio routing and AI-yield controls are isolated strictly per call."""
    call_id_1 = f"call_{uuid.uuid4().hex[:8]}"
    call_id_2 = f"call_{uuid.uuid4().hex[:8]}"

    # Initialize two distinct call sessions
    session_1 = await _get_or_create_session(call_id_1)
    session_2 = await _get_or_create_session(call_id_2)

    assert session_1 is not session_2
    assert "ai_rtp_tx_queue" in session_1
    assert "ai_rtp_tx_queue" in session_2

    # Pre-populate session 1 with bot audio
    session_1["ai_rtp_tx_queue"].append(b"\x00" * 160)
    session_1["ai_speaking"] = True

    # Supervisor barges into call 1
    session_1["supervisor_barging"] = True
    pause_sip_ai_bot(call_id_1)

    # Verify AI speech is paused and AI audio queue cleared on call 1
    assert session_1["ai_speaking"] is False
    assert len(session_1["ai_rtp_tx_queue"]) == 0

    # Verify Call 2 was completely unaffected
    assert session_2.get("supervisor_barging") is not True

    # Supervisor speaks into call 1: send 160-byte RTP frame
    supervisor_mic_frame = b"\x7F" * 160
    session_1["ai_rtp_tx_queue"].append(supervisor_mic_frame)

    assert len(session_1["ai_rtp_tx_queue"]) == 1
    assert len(session_2["ai_rtp_tx_queue"]) == 0, "Audio leaked into call 2!"

    # Supervisor disconnects
    session_1["supervisor_barging"] = False
    resume_sip_ai_bot(call_id_1)
    assert session_1.get("supervisor_barging") is False

    # Cleanup sessions
    session_1["running"] = False
    session_2["running"] = False
    barge_sessions.pop(call_id_1, None)
    barge_sessions.pop(call_id_2, None)


@pytest.mark.asyncio
async def test_audit_log_persistence(setup_test_campaign):
    """Phase 7: Verify persistent AuditLog entries for listen and barge interventions."""
    from app.services.audit_service import log_action

    ctx = setup_test_campaign
    call_id = str(uuid.uuid4())

    # Log LISTEN_START
    rec_listen = await log_action(
        action="CALL_LISTEN_START",
        entity="Call",
        tenant_id=ctx["biz_id"],
        actor=ctx["supervisor_id"],
        entity_id=call_id,
        metadata={"mode": "LISTEN", "role": "SUPERVISOR"}
    )
    assert rec_listen is not None
    assert rec_listen.action == "CALL_LISTEN_START"

    # Log BARGE_IN_START
    rec_barge = await log_action(
        action="CALL_BARGE_IN_START",
        entity="Call",
        tenant_id=ctx["biz_id"],
        actor=ctx["admin_id"],
        entity_id=call_id,
        metadata={"mode": "BARGE_IN", "role": "ADMIN"}
    )
    assert rec_barge is not None
    assert rec_barge.action == "CALL_BARGE_IN_START"

    # Query from DB to verify persistence
    async with AsyncSessionLocal() as db:
        stmt = select(AuditLog).where(
            AuditLog.entity_id == call_id,
            AuditLog.business_id == ctx["biz_id"]
        ).order_by(AuditLog.created_at.asc())
        logs = list((await db.scalars(stmt)).all())

        assert len(logs) == 2
        assert logs[0].action == "CALL_LISTEN_START"
        assert logs[0].user_id == ctx["supervisor_id"]
        assert logs[1].action == "CALL_BARGE_IN_START"
        assert logs[1].user_id == ctx["admin_id"]


@pytest.mark.asyncio
async def test_live_monitor_active_calls_and_transcript_isolation(setup_test_campaign):
    """Phase 8 & Live Monitor: Verify /api/voice/calls/active endpoint, role authorization, and transcript isolation."""
    from httpx import AsyncClient, ASGITransport
    from app.main import app
    from app.core.security import create_access_token
    from app.db.models import CallMessage

    ctx = setup_test_campaign
    sup_token = create_access_token(ctx["supervisor_id"], "SUPERVISOR")
    viewer_token = create_access_token(ctx["viewer_id"], "VIEWER")

    async with AsyncSessionLocal() as db:
        # Create campaign and leads
        camp = Campaign(
            name="Live Monitor Campaign",
            business_id=ctx["biz_id"],
            status="RUNNING"
        )
        db.add(camp)
        await db.flush()

        lead1 = Lead(first_name="Alice", last_name="Smith", phone="+19990001", business_id=ctx["biz_id"])
        lead2 = Lead(first_name="Bob", last_name="Jones", phone="+19990002", business_id=ctx["biz_id"])
        db.add_all([lead1, lead2])
        await db.flush()

        # Create 2 concurrent active calls with independent transcripts
        call1 = Call(
            business_id=ctx["biz_id"],
            campaign_id=camp.id,
            lead_id=lead1.id,
            agent_id=ctx["agent_id"],
            direction="OUTBOUND",
            status="CONNECTED",
            to_number=lead1.phone,
            transcript="AI: Hello Alice!\nCustomer: Hi, I want to book an appointment.",
        )
        call2 = Call(
            business_id=ctx["biz_id"],
            campaign_id=camp.id,
            lead_id=lead2.id,
            agent_id=ctx["agent_id"],
            direction="OUTBOUND",
            status="TALKING",
            to_number=lead2.phone,
            transcript="AI: Hello Bob!\nCustomer: Sorry, not interested.",
        )
        db.add_all([call1, call2])
        await db.flush()

        # Persist discrete CallMessage rows
        db.add(CallMessage(call_id=call1.id, sender_type="AGENT", content="Hello Alice!"))
        db.add(CallMessage(call_id=call1.id, sender_type="CUSTOMER", content="Hi, I want to book an appointment."))
        db.add(CallMessage(call_id=call2.id, sender_type="AGENT", content="Hello Bob!"))
        db.add(CallMessage(call_id=call2.id, sender_type="CUSTOMER", content="Sorry, not interested."))
        await db.commit()

        call1_id = call1.id
        call2_id = call2.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # 1. Unauthorized VIEWER role gets 403
        res_viewer = await client.get(
            "/api/voice/calls/active",
            headers={"Authorization": f"Bearer {viewer_token}"}
        )
        assert res_viewer.status_code == 403, f"Expected 403 for VIEWER, got {res_viewer.status_code}"

        # 2. Authorized SUPERVISOR role gets 200 and list of active calls
        res_sup = await client.get(
            "/api/voice/calls/active",
            headers={"Authorization": f"Bearer {sup_token}"}
        )
        assert res_sup.status_code == 200
        active_list = res_sup.json()
        assert isinstance(active_list, list)

        call_map = {c["call_id"]: c for c in active_list if c["call_id"] in (call1_id, call2_id)}
        assert call1_id in call_map
        assert call2_id in call_map

        # Verify call 1 metadata and isolated transcript preview
        c1_data = call_map[call1_id]
        assert c1_data["lead_name"] == "Alice Smith"
        assert c1_data["campaign_name"] == "Live Monitor Campaign"
        assert c1_data["status"] == "CONNECTED"
        assert "book an appointment" in c1_data["last_message"]
        assert "not interested" not in c1_data["last_message"]

        # Verify call 2 metadata and isolated transcript preview
        c2_data = call_map[call2_id]
        assert c2_data["lead_name"] == "Bob Jones"
        assert c2_data["status"] == "TALKING"
        assert "not interested" in c2_data["last_message"]
        assert "book an appointment" not in c2_data["last_message"]

    # Verify per-call CallMessage rows in DB are isolated
    async with AsyncSessionLocal() as db:
        c1_msgs = (await db.scalars(select(CallMessage).where(CallMessage.call_id == call1_id).order_by(CallMessage.timestamp.asc()))).all()
        c2_msgs = (await db.scalars(select(CallMessage).where(CallMessage.call_id == call2_id).order_by(CallMessage.timestamp.asc()))).all()

        assert len(c1_msgs) == 2
        assert len(c2_msgs) == 2
        assert c1_msgs[0].content == "Hello Alice!"
        assert c1_msgs[1].content == "Hi, I want to book an appointment."
        assert c2_msgs[0].content == "Hello Bob!"
        assert c2_msgs[1].content == "Sorry, not interested."

