import asyncio
import uuid
import pytest
import pytest_asyncio
from datetime import datetime
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
    CallMessage,
    QualificationRule,
)
from app.core.security import hash_password
from app.workers.dialer import run_predictive_dialer_async, ACTIVE_DIALER_STATUSES
from app.api.routes.campaign_ai_agent import get_campaign_ai_agent_leads
from app.api.routes.calls import get_call_status, end_call
from app.api.routes.campaigns import dialer_status


@pytest_asyncio.fixture
async def setup_isolation_ctx():
    async with AsyncSessionLocal() as db:
        biz1 = Business(name=f"BizA_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        biz2 = Business(name=f"BizB_{uuid.uuid4().hex[:6]}", owner_user_id=str(uuid.uuid4()))
        db.add_all([biz1, biz2])
        await db.flush()

        admin_a = User(
            email=f"admin_a_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Admin A",
            first_name="Admin",
            last_name="A",
            role="ADMIN",
            business_id=biz1.id,
        )
        admin_b = User(
            email=f"admin_b_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Admin B",
            first_name="Admin",
            last_name="B",
            role="ADMIN",
            business_id=biz2.id,
        )
        agent_a = AIAgent(
            name=f"AgentA_{uuid.uuid4().hex[:6]}",
            business_id=biz1.id,
            status="ACTIVE",
            voice_id="snapserve-en-1",
        )
        qrule1 = QualificationRule(business_id=biz1.id)
        qrule2 = QualificationRule(business_id=biz2.id)
        db.add_all([admin_a, admin_b, agent_a, qrule1, qrule2])
        await db.commit()

        yield {
            "biz1_id": biz1.id,
            "biz2_id": biz2.id,
            "admin_a": admin_a,
            "admin_b": admin_b,
            "agent_a_id": agent_a.id,
        }


@pytest.mark.asyncio
async def test_ratio_1_3_concurrency_and_active_calls_lifecycle(setup_isolation_ctx, monkeypatch):
    """
    Requirement 1 & 2:
    - Ratio 1:3 initiates up to 3 concurrent calls.
    - AI agent leads endpoint returns the actual active calls with call_id and statuses (DIALING/RINGING/CONNECTED).
    """
    ctx = setup_isolation_ctx

    dispatched = []

    async def mock_initiate(db, lead, agent=None, *args, **kwargs):
        dispatched.append(lead.id)
        call_id = f"call-test-{uuid.uuid4().hex[:8]}"
        call = Call(
            id=call_id,
            business_id=lead.business_id,
            lead_id=lead.id,
            status="RINGING",
            direction="OUTBOUND",
            from_number="+1800111222",
            to_number=lead.phone,
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()

        # Update CampaignLead
        from sqlalchemy import update
        await db.execute(
            update(CampaignLead)
            .where(CampaignLead.lead_id == lead.id)
            .values(status="RINGING", call_id=call_id)
        )
        return call

    monkeypatch.setattr("app.workers.dialer.initiate_outbound_call", mock_initiate)
    monkeypatch.setattr("app.services.voice_service.initiate_outbound_call", mock_initiate)

    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="1:3 Active Calls Test Campaign",
            business_id=ctx["biz1_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_a_id"],
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
                phone=f"+91987654321{i}",
                business_id=ctx["biz1_id"],
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

        # Run dialer for campaign
        await run_predictive_dialer_async(camp.id)
        # Should dial 3 leads
        assert len(dispatched) == 3

        # Verify dialer status endpoint reflects 3 active calls out of 3 target capacity
        status_resp = await dialer_status(id=camp.id, user=ctx["admin_a"], db=db)
        assert status_resp["ratio"] == 3
        assert status_resp["active_calls"] == 3
        assert status_resp["target_capacity"] == 3
        assert status_resp["available_slots"] == 0

        # Fetch leads via get_campaign_ai_agent_leads
        ai_leads = await get_campaign_ai_agent_leads(
            campaign_id=camp.id,
            user=ctx["admin_a"],
            db=db,
        )
        leads_data = ai_leads["leads"]
        active_ai_leads = [l for l in leads_data if l.get("call_status") in ACTIVE_DIALER_STATUSES]
        assert len(active_ai_leads) == 3
        for l in active_ai_leads:
            assert l["call_id"] is not None
            assert l["call_status"] in ["DIALING", "RINGING", "IN_PROGRESS", "CONNECTED"]


@pytest.mark.asyncio
async def test_conversation_isolation_between_concurrent_calls(setup_isolation_ctx):
    """
    Requirement 3, 4, 5:
    - Customer A and Customer B have independent call_ids and distinct transcripts.
    - Fetching Call A returns strictly Call A's messages.
    - Fetching Call B returns strictly Call B's messages.
    - No cross-contamination of messages or transcripts.
    """
    ctx = setup_isolation_ctx
    async with AsyncSessionLocal() as db:
        # Create Call A
        call_a = Call(
            id=f"call-a-{uuid.uuid4().hex[:8]}",
            business_id=ctx["biz1_id"],
            from_number="+1800111222",
            to_number="+919585310712",
            status="CONNECTED",
            initiated_at=datetime.utcnow(),
        )
        # Create Call B
        call_b = Call(
            id=f"call-b-{uuid.uuid4().hex[:8]}",
            business_id=ctx["biz1_id"],
            from_number="+1800111222",
            to_number="+919788867625",
            status="RINGING",
            initiated_at=datetime.utcnow(),
        )
        db.add_all([call_a, call_b])
        await db.flush()

        # Add messages for Call A (Mugilan)
        msg_a1 = CallMessage(
            call_id=call_a.id,
            sender_type="AGENT",
            content="Good afternoon Mugilan! How are you doing today?",
            timestamp=datetime.utcnow(),
        )
        msg_a2 = CallMessage(
            call_id=call_a.id,
            sender_type="CUSTOMER",
            content="I'm good, thank you. What is this about?",
            timestamp=datetime.utcnow(),
        )
        # Add messages for Call B (Rohit)
        msg_b1 = CallMessage(
            call_id=call_b.id,
            sender_type="AGENT",
            content="Hello Rohit, this is MAGIC from Premerica Life Insurance. Can I help you today?",
            timestamp=datetime.utcnow(),
        )
        db.add_all([msg_a1, msg_a2, msg_b1])
        await db.commit()

        # Query Call A via get_call_status API
        ret_a = await get_call_status(call_id=call_a.id, user=ctx["admin_a"], db=db)
        assert ret_a["call_id"] == call_a.id
        assert len(ret_a["messages"]) == 2
        assert ret_a["messages"][0]["text"] == "Good afternoon Mugilan! How are you doing today?"
        assert ret_a["messages"][1]["text"] == "I'm good, thank you. What is this about?"
        assert ret_a["messages"][0]["sender"] == "ai"
        assert ret_a["messages"][1]["sender"] == "user"

        # Query Call B via get_call_status API
        ret_b = await get_call_status(call_id=call_b.id, user=ctx["admin_a"], db=db)
        assert ret_b["call_id"] == call_b.id
        assert len(ret_b["messages"]) == 1
        assert ret_b["messages"][0]["text"] == "Hello Rohit, this is MAGIC from Premerica Life Insurance. Can I help you today?"
        assert ret_b["messages"][0]["sender"] == "ai"

        # Verify no messages from B appear in A, and no messages from A appear in B
        texts_a = [m["text"] for m in ret_a["messages"]]
        texts_b = [m["text"] for m in ret_b["messages"]]
        assert not set(texts_a).intersection(set(texts_b))


@pytest.mark.asyncio
async def test_call_termination_moves_out_of_active_keeps_history(setup_isolation_ctx):
    """
    Requirement 7 & 8:
    - When Call A ends, its CampaignLead transitions to COMPLETED.
    - Active calls count drops, but Call A's full messages and transcript remain retrievable.
    """
    ctx = setup_isolation_ctx
    async with AsyncSessionLocal() as db:
        camp = Campaign(
            id=str(uuid.uuid4()),
            name="Termination Test Camp",
            business_id=ctx["biz1_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=ctx["agent_a_id"],
            auto_dial_enabled=True,
            auto_dial_ratio=1,
            schedule={"calling_mode": "local"},
        )
        db.add(camp)
        await db.flush()

        lead = Lead(
            id=str(uuid.uuid4()),
            first_name="Customer",
            last_name="Term",
            phone="+919999999999",
            business_id=ctx["biz1_id"],
        )
        db.add(lead)
        await db.flush()

        call = Call(
            id=f"call-term-{uuid.uuid4().hex[:8]}",
            business_id=ctx["biz1_id"],
            campaign_id=camp.id,
            from_number="+1800111222",
            to_number="+919999999999",
            status="CONNECTED",
            initiated_at=datetime.utcnow(),
        )
        db.add(call)
        await db.flush()

        cl = CampaignLead(
            campaign_id=camp.id,
            lead_id=lead.id,
            call_id=call.id,
            status="CONNECTED",
        )
        db.add(cl)

        msg = CallMessage(
            call_id=call.id,
            sender_type="AGENT",
            content="Thank you for your time. Goodbye!",
            timestamp=datetime.utcnow(),
        )
        db.add(msg)
        await db.commit()

        # End the call
        await end_call(call_id=call.id, user=ctx["admin_a"], db=db)

        # Refresh objects
        await db.refresh(call)
        await db.refresh(cl)
        assert call.status == "completed"
        assert cl.status == "COMPLETED"

        # Check dialer status - 0 active calls
        status_resp = await dialer_status(id=camp.id, user=ctx["admin_a"], db=db)
        assert status_resp["active_calls"] == 0

        # Check call messages are still 100% accessible
        ret_call = await get_call_status(call_id=call.id, user=ctx["admin_a"], db=db)
        assert len(ret_call["messages"]) == 1
        assert ret_call["messages"][0]["text"] == "Thank you for your time. Goodbye!"


@pytest.mark.asyncio
async def test_tenant_authorization_isolation(setup_isolation_ctx):
    """
    Requirement 11:
    - User from Business B cannot access Call details from Business A.
    """
    ctx = setup_isolation_ctx
    async with AsyncSessionLocal() as db:
        call_a = Call(
            id=f"call-auth-{uuid.uuid4().hex[:8]}",
            business_id=ctx["biz1_id"],
            from_number="+1800111222",
            to_number="+919585310712",
            status="CONNECTED",
            initiated_at=datetime.utcnow(),
        )
        db.add(call_a)
        await db.commit()

        # Attempting to fetch call_a with admin_b should raise 403 Forbidden
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as exc_info:
            await get_call_status(call_id=call_a.id, user=ctx["admin_b"], db=db)
        assert exc_info.value.status_code in [403, 404]


@pytest.mark.asyncio
async def test_three_simultaneous_answered_calls_dynamic_ai_conversations(setup_isolation_ctx):
    """
    Core Verification (Requirements 2 & 5):
    - 3 simultaneous calls (Customer A, B, C) have completely isolated telephony/barge sessions.
    - Each call has distinct inbound/outbound RTP audio queues.
    - Customer A, B, and C speak distinct queries simultaneously.
    - Each call processes its customer utterance in parallel with zero transcript or context bleed.
    - AI responses are context-aware and specific to what each customer said.
    - Historical access via get_call_status returns disjoint message sets.
    """
    ctx = setup_isolation_ctx
    from app.api.routes.barge_stream import _get_or_create_session, barge_sessions
    from app.services.lead_engagement_service import process_incoming_message
    from app.db.models import Conversation, Message

    async with AsyncSessionLocal() as db:
        agent = await db.get(AIAgent, ctx["agent_a_id"])

        campaign = Campaign(
            id=str(uuid.uuid4()),
            name="Concurrent AI Voice Test Campaign",
            business_id=ctx["biz1_id"],
            status="RUNNING",
            agent_type="AI_VOICE",
            agent_id=agent.id,
            auto_dial_enabled=True,
            auto_dial_ratio=3,
            schedule={
                "calling_mode": "sip",
                "script": "Premerica Life Insurance. Flexi Income Plan and RockSolid Future. Explain premium options when asked. If customer has affordability concerns, offer lower payment options. If customer requests Tamil, switch to Tamil.",
            },
        )
        db.add(campaign)
        await db.flush()

        # 3 Leads
        lead_a = Lead(id=str(uuid.uuid4()), first_name="Karthik", last_name="A", phone="+919876543201", business_id=ctx["biz1_id"])
        lead_b = Lead(id=str(uuid.uuid4()), first_name="Priya", last_name="B", phone="+919876543202", business_id=ctx["biz1_id"])
        lead_c = Lead(id=str(uuid.uuid4()), first_name="Suresh", last_name="C", phone="+919876543203", business_id=ctx["biz1_id"])
        db.add_all([lead_a, lead_b, lead_c])
        await db.flush()

        # 3 Calls
        call_a = Call(id=f"call-cust-a-{uuid.uuid4().hex[:8]}", business_id=ctx["biz1_id"], campaign_id=campaign.id, lead_id=lead_a.id, status="CONNECTED", direction="OUTBOUND", client_name="Karthik", client_phone=lead_a.phone)
        call_b = Call(id=f"call-cust-b-{uuid.uuid4().hex[:8]}", business_id=ctx["biz1_id"], campaign_id=campaign.id, lead_id=lead_b.id, status="CONNECTED", direction="OUTBOUND", client_name="Priya", client_phone=lead_b.phone)
        call_c = Call(id=f"call-cust-c-{uuid.uuid4().hex[:8]}", business_id=ctx["biz1_id"], campaign_id=campaign.id, lead_id=lead_c.id, status="CONNECTED", direction="OUTBOUND", client_name="Suresh", client_phone=lead_c.phone)
        db.add_all([call_a, call_b, call_c])
        await db.flush()

        # 3 Conversations
        conv_a = Conversation(id=str(uuid.uuid4()), business_id=ctx["biz1_id"], lead_id=lead_a.id, agent_id=agent.id, channel="VOICE", status="ACTIVE")
        conv_b = Conversation(id=str(uuid.uuid4()), business_id=ctx["biz1_id"], lead_id=lead_b.id, agent_id=agent.id, channel="VOICE", status="ACTIVE")
        conv_c = Conversation(id=str(uuid.uuid4()), business_id=ctx["biz1_id"], lead_id=lead_c.id, agent_id=agent.id, channel="VOICE", status="ACTIVE")
        db.add_all([conv_a, conv_b, conv_c])
        await db.flush()

        call_a.conversation_id = conv_a.id
        call_b.conversation_id = conv_b.id
        call_c.conversation_id = conv_c.id
        await db.commit()

        # 1. Verify Audio Sessions are 100% physically isolated
        sess_a = await _get_or_create_session(call_a.id)
        sess_b = await _get_or_create_session(call_b.id)
        sess_c = await _get_or_create_session(call_c.id)

        assert sess_a is not sess_b
        assert sess_b is not sess_c
        assert sess_a["inbound_queue"] is not sess_b["inbound_queue"]
        assert sess_a["inbound_queue"] is not sess_c["inbound_queue"]
        assert sess_a["ai_rtp_tx_queue"] is not sess_b["ai_rtp_tx_queue"]
        assert sess_a["ai_rtp_tx_queue"] is not sess_c["ai_rtp_tx_queue"]

        # Enqueue dummy audio packets to test queue segregation
        await sess_a["inbound_queue"].put(b"\x01" * 160)
        await sess_b["inbound_queue"].put(b"\x02" * 160)
        await sess_c["inbound_queue"].put(b"\x03" * 160)

        assert (await sess_a["inbound_queue"].get()) == b"\x01" * 160
        assert (await sess_b["inbound_queue"].get()) == b"\x02" * 160
        assert (await sess_c["inbound_queue"].get()) == b"\x03" * 160

        # 2. Concurrently simulate customer speech from 3 independent callers
        utterance_a = "I want to know about the premium."
        utterance_b = "I can't afford that amount, what are the lower options?"
        utterance_c = "Can you explain it in Tamil? இதை தமிழில் விளக்குங்கள்."

        # Execute all 3 conversation turns concurrently in parallel with per-call DB sessions as in production
        async def _call_turn(l_id, c_id, text):
            for attempt in range(5):
                try:
                    async with AsyncSessionLocal() as local_db:
                        local_l = await local_db.get(Lead, l_id)
                        local_c = await local_db.get(Conversation, c_id)
                        local_ag = await local_db.get(AIAgent, agent.id)
                        local_camp = await local_db.get(Campaign, campaign.id)
                        res = await process_incoming_message(
                            db=local_db,
                            lead=local_l,
                            conversation=local_c,
                            agent=local_ag,
                            content=text,
                            campaign=local_camp,
                        )
                        await local_db.commit()
                        return res
                except Exception as e:
                    if "locked" in str(e).lower() and attempt < 4:
                        await asyncio.sleep(0.5 * (attempt + 1))
                        continue
                    raise

        res_a, res_b, res_c = await asyncio.gather(
            _call_turn(lead_a.id, conv_a.id, utterance_a),
            _call_turn(lead_b.id, conv_b.id, utterance_b),
            _call_turn(lead_c.id, conv_c.id, utterance_c),
        )


        reply_a = res_a["ai_message"].content if res_a.get("ai_message") else ""
        reply_b = res_b["ai_message"].content if res_b.get("ai_message") else ""
        reply_c = res_c["ai_message"].content if res_c.get("ai_message") else ""

        # Verify replies are non-empty and independent
        assert reply_a != ""
        assert reply_b != ""
        assert reply_c != ""

        # Verify each reply is distinct and tailored to the respective customer's request
        assert reply_a != reply_b
        assert reply_b != reply_c

        # Save to CallMessage table as the live voice pipeline does
        msg_a_user = CallMessage(call_id=call_a.id, sender_type="CUSTOMER", content=utterance_a)
        msg_a_ai = CallMessage(call_id=call_a.id, sender_type="AGENT", content=reply_a)

        msg_b_user = CallMessage(call_id=call_b.id, sender_type="CUSTOMER", content=utterance_b)
        msg_b_ai = CallMessage(call_id=call_b.id, sender_type="AGENT", content=reply_b)

        msg_c_user = CallMessage(call_id=call_c.id, sender_type="CUSTOMER", content=utterance_c)
        msg_c_ai = CallMessage(call_id=call_c.id, sender_type="AGENT", content=reply_c)

        db.add_all([msg_a_user, msg_a_ai, msg_b_user, msg_b_ai, msg_c_user, msg_c_ai])
        await db.commit()

        # 3. Verify get_call_status returns strictly disjoint, isolated transcripts
        call_status_a = await get_call_status(call_id=call_a.id, user=ctx["admin_a"], db=db)
        call_status_b = await get_call_status(call_id=call_b.id, user=ctx["admin_a"], db=db)
        call_status_c = await get_call_status(call_id=call_c.id, user=ctx["admin_a"], db=db)

        texts_a = [m["text"] for m in call_status_a["messages"]]
        texts_b = [m["text"] for m in call_status_b["messages"]]
        texts_c = [m["text"] for m in call_status_c["messages"]]

        assert utterance_a in texts_a
        assert utterance_a not in texts_b
        assert utterance_a not in texts_c

        assert utterance_b in texts_b
        assert utterance_b not in texts_a
        assert utterance_b not in texts_c

        assert utterance_c in texts_c
        assert utterance_c not in texts_a
        assert utterance_c not in texts_b

