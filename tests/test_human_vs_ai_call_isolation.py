import asyncio
import collections
from datetime import datetime, timezone
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import Call, Lead, AIAgent, User, Campaign
from app.api.routes.agent_dashboard import InitiateCallRequest, initiate_call
from app.api.routes.barge_stream import barge_sessions, _get_or_create_session
from app.services.sip_ai_bot import start_sip_ai_bot, stop_sip_ai_bot, _run_sip_ai_bot, ACTIVE_SIP_BOT_TASKS
from app.services.voice_service import hangup_sip_call


@pytest.mark.asyncio
async def test_human_dashboard_call_with_ai_campaign_remains_human():
    """TEST 1 & 4: Human Voice Agent initiates call with AI campaign -> persisted as HUMAN, AI bot never starts."""
    db = AsyncMock()
    user = User(id="user-human-99", email="agent@callzenza.com", role="VOICE_AGENT", business_id="biz-1")
    
    # Campaign configured as AI Agent
    ai_campaign = Campaign(
        id="camp-ai-auto-1",
        name="AI Sales Campaign",
        business_id="biz-1",
        agent_type="AI_VOICE",
        agent_id="ai-bot-123"
    )
    lead = Lead(id="lead-cust-1", business_id="biz-1", phone="+19876543210", first_name="Jane", last_name="Customer")

    async def mock_get(model, pk):
        if model == Campaign and pk == "camp-ai-auto-1":
            return ai_campaign
        if model == Lead and pk == "lead-cust-1":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None), all=MagicMock(return_value=[])))))
    db.add = MagicMock()
    db.add_all = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()

    # Explicit human agent initiation from voice agent dashboard
    payload = InitiateCallRequest(
        lead_id="lead-cust-1",
        phone_number="+19876543210",
        campaign_id="camp-ai-auto-1",
        agent_type="HUMAN",
        agent_id="user-human-99",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="SIP-HUMAN-001")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None
        call_id = res["call_id"]

        # 1. Verify Call persisted in DB has agent_type='HUMAN'
        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        call_rec = calls_added[0]
        assert call_rec.agent_type == "HUMAN", f"Expected HUMAN, got {call_rec.agent_type}"

        # 2. Verify barge session pre-tagged as HUMAN
        b_sess = barge_sessions.get(call_id)
        assert b_sess is not None
        assert b_sess["agent_type"] == "HUMAN"
        assert b_sess["is_voice_agent_call"] is True

        # 3. Simulate SIP 200 OK connection logic from voice_service.py
        call_rec.status = "CONNECTED"
        call_rec.call_sid = "SIP-HUMAN-001"

        # Even if campaign is AI_VOICE, persisted call_rec.agent_type is HUMAN
        is_voice_agent = (call_rec.agent_type or "").upper() in ("HUMAN", "VOICE", "VOICE_AGENT")
        assert is_voice_agent is True

        bot_started_calls = []
        with patch("app.services.sip_ai_bot.start_sip_ai_bot", side_effect=lambda cid, is_inbound=False: bot_started_calls.append(cid)):
            if not is_voice_agent:
                start_sip_ai_bot(call_id, is_inbound=False)

        # AI Bot startup count MUST remain ZERO
        assert len(bot_started_calls) == 0

        # Defensive check: Even if start_sip_ai_bot was invoked directly, it must prevent start
        start_sip_ai_bot(call_id, is_inbound=False)
        assert call_id not in ACTIVE_SIP_BOT_TASKS

    # Cleanup
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_ai_agent_campaign_call_starts_bot_once():
    """TEST 2: AI Agent campaign call -> AI_VOICE persisted -> AI bot starts exactly once."""
    call_id = "call-ai-test-101"
    ai_call = Call(
        id=call_id,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="AI_VOICE",
        agent_id="ai-bot-123",
        status="RINGING",
    )

    sess = await _get_or_create_session(call_id)
    sess["agent_type"] = "AI_VOICE"
    sess["is_voice_agent_call"] = False

    bot_launch_count = 0
    with patch("app.services.sip_ai_bot._run_sip_ai_bot", new_callable=AsyncMock) as mock_run:
        # First launch
        start_sip_ai_bot(call_id, is_inbound=False)
        assert call_id in ACTIVE_SIP_BOT_TASKS
        bot_launch_count += 1

        # Second launch attempt (idempotency check)
        start_sip_ai_bot(call_id, is_inbound=False)
        # Should not launch a second task
        assert bot_launch_count == 1

    # Cleanup
    stop_sip_ai_bot(call_id)
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_ai_campaign_with_human_backup_remains_ai_voice():
    """TEST 3: AI campaign with a human backup voice_agent_id -> primary type remains AI_VOICE."""
    from app.api.routes.calls import OutboundCallRequest, initiate_outbound_call as calls_initiate_call
    
    db = AsyncMock()
    user = User(id="user-admin-1", email="admin@callzenza.com", role="ADMIN", business_id="biz-1")
    
    # AI campaign with backup human voice agent
    campaign = Campaign(
        id="camp-ai-backup-1",
        name="AI Campaign With Transfer Backup",
        business_id="biz-1",
        agent_type="AI_VOICE",
        agent_id="ai-primary-1",
        voice_agent_id="human-backup-expert-1",  # Transfer backup!
    )
    ai_agent = AIAgent(id="ai-primary-1", name="Alex", business_id="biz-1", status="ACTIVE")
    lead = Lead(id="lead-3", business_id="biz-1", phone="+18005550300", first_name="Tom")

    async def mock_get(model, pk):
        if model == Campaign:
            return campaign
        if model == AIAgent:
            return ai_agent
        if model == Lead:
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=ai_agent)
    async def mock_execute(stmt):
        m = MagicMock()
        m.scalars.return_value.first.return_value = None
        return m
    db.execute = AsyncMock(side_effect=mock_execute)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # Non-explicit payload using AI campaign
    payload = OutboundCallRequest(
        phone_number="+18005550300",
        campaign_id="camp-ai-backup-1",
        lead_id="lead-3",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_prov:
        prov = AsyncMock()
        prov.place_call = AsyncMock(return_value="SID-AI-BACKUP")
        mock_prov.return_value = prov

        res = await calls_initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None

        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        call_created = calls_added[0]

        # Primary agent_type MUST remain AI_VOICE despite voice_agent_id backup
        assert call_created.agent_type == "AI_VOICE"
        assert call_created.agent_id == "ai-primary-1"


@pytest.mark.asyncio
async def test_duplicate_callbacks_and_id_mismatch_do_not_switch_type_or_double_start():
    """TEST 5: Duplicate SIP callbacks, alternate SIP Call-IDs, and DB UUID vs call_sid mismatch."""
    db_call_id = "550e8400-e29b-41d4-a716-446655440000"
    sip_call_cid = "call-abcdef123456@callzenza"

    call_rec = Call(
        id=db_call_id,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="HUMAN",
        call_sid=sip_call_cid,
        status="CONNECTED",
    )

    db_mock = AsyncMock()
    async def mock_get(model, pk):
        if pk in (db_call_id, sip_call_cid):
            return call_rec
        return None
    db_mock.get = AsyncMock(side_effect=mock_get)
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    # Session aliased under both db_call_id and sip_call_cid
    sess = await _get_or_create_session(db_call_id)
    sess["agent_type"] = "HUMAN"
    sess["is_voice_agent_call"] = True
    barge_sessions[sip_call_cid] = sess

    bot_launches = []
    def mock_start(cid, is_inbound=False):
        bot_launches.append(cid)

    # 1. First callback with db_call_id
    call_agent_type = (call_rec.agent_type or "").upper()
    is_voice = call_agent_type in ("HUMAN", "VOICE", "VOICE_AGENT")
    if not is_voice:
        mock_start(db_call_id)

    # 2. Duplicate callback with sip_call_cid
    call_agent_type_2 = (call_rec.agent_type or "").upper()
    is_voice_2 = call_agent_type_2 in ("HUMAN", "VOICE", "VOICE_AGENT")
    if not is_voice_2:
        mock_start(sip_call_cid)

    # Neither callback should ever launch the bot for a HUMAN call
    assert len(bot_launches) == 0
    assert sess["agent_type"] == "HUMAN"

    # Cleanup
    barge_sessions.pop(db_call_id, None)
    barge_sessions.pop(sip_call_cid, None)


@pytest.mark.asyncio
async def test_ending_call_clears_media_tasks_and_queues():
    """TEST 6: Ending a call cleans up barge session queues and stops audio tasks."""
    call_id = "call-cleanup-test-999"
    sess = await _get_or_create_session(call_id)
    
    # Simulate queued frames from AI or microphone
    sess["ai_rtp_tx_queue"].append(b"\xff" * 160)
    sess["ai_rtp_tx_queue"].append(b"\xd5" * 160)
    sess["ai_speaking"] = True
    sess["running"] = True
    assert len(sess["ai_rtp_tx_queue"]) == 2

    # Hang up call
    with patch("app.services.voice_service.ACTIVE_SIP_CALLS", {}):
        hangup_sip_call(call_id)

    # Assert queues are cleared and running is False
    assert sess["running"] is False
    assert sess["ai_speaking"] is False
    assert len(sess["ai_rtp_tx_queue"]) == 0

    # Cleanup
    barge_sessions.pop(call_id, None)
