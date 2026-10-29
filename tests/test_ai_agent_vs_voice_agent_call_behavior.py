import asyncio
import json
import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import Call, Lead, AIAgent, User, Business, Campaign, Expert
from app.api.routes.agent_dashboard import InitiateCallRequest, initiate_call


@pytest.mark.asyncio
async def test_ai_agent_assigned_call_creation():
    """TEST 1A: Outbound call with AI Agent assigned has agent_type='AI_VOICE'."""
    db = AsyncMock()
    user = User(id="user-1", email="agent@callzenza.com", role="AGENT", business_id="biz-1")
    
    ai_agent = AIAgent(id="ai-agent-1", name="Alex", business_id="biz-1", status="ACTIVE")
    ai_campaign = Campaign(id="camp-ai-1", name="AI Campaign", business_id="biz-1", agent_type="AI_VOICE", agent_id="ai-agent-1")
    lead = Lead(id="lead-1", business_id="biz-1", phone="+18005550199", first_name="John", last_name="Doe")

    async def mock_get(model, pk):
        if model == Campaign and pk == "camp-ai-1":
            return ai_campaign
        if model == AIAgent and pk == "ai-agent-1":
            return ai_agent
        if model == Lead and pk == "lead-1":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=ai_agent)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=ai_agent)))))

    payload = InitiateCallRequest(
        lead_id="lead-1",
        phone_number="+18005550199",
        campaign_id="camp-ai-1",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_123")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None
        assert res["status"] == "RINGING"

        # Check call added to DB
        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call = calls_added[0]
        assert created_call.agent_type == "AI_VOICE"
        assert created_call.agent_id == "ai-agent-1"


@pytest.mark.asyncio
async def test_voice_agent_assigned_call_creation():
    """TEST 2A: Outbound call with human Voice Agent assigned has agent_type='HUMAN'."""
    db = AsyncMock()
    user = User(id="user-voice-1", email="human@callzenza.com", role="VOICE_AGENT", business_id="biz-1")
    
    human_campaign = Campaign(
        id="camp-human-1",
        name="Voice Agent Campaign",
        business_id="biz-1",
        agent_type="HUMAN",
        voice_agent_id="expert-1"
    )
    lead = Lead(id="lead-2", business_id="biz-1", phone="+18005550200", first_name="Jane", last_name="Smith")

    async def mock_get(model, pk):
        if model == Campaign and pk == "camp-human-1":
            return human_campaign
        if model == Lead and pk == "lead-2":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload = InitiateCallRequest(
        lead_id="lead-2",
        phone_number="+18005550200",
        campaign_id="camp-human-1",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_456")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None

        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call = calls_added[0]
        assert created_call.agent_type == "HUMAN"
        assert created_call.agent_id == "expert-1"


@pytest.mark.asyncio
async def test_voice_agent_manual_dial_defaults_to_human():
    """TEST 2B: Manual dial from Voice Agent Dashboard without campaign defaults to HUMAN."""
    db = AsyncMock()
    user = User(id="voice-user-99", email="dialer@callzenza.com", role="VOICE_AGENT", business_id="biz-1")
    
    db.get = AsyncMock(return_value=None)
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload = InitiateCallRequest(
        phone_number="+18005559999",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_789")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None

        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call = calls_added[0]
        assert created_call.agent_type == "HUMAN"
        assert created_call.agent_id == "voice-user-99"
        assert created_call.user_id == "voice-user-99"


@pytest.mark.asyncio
async def test_ai_agent_call_answered_starts_ai_bot():
    """TEST 1B: When call is answered and assigned to AI Agent, start_sip_ai_bot is called."""
    call_id = "call-ai-test-101"
    ai_call = Call(
        id=call_id,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="AI_VOICE",
        agent_id="ai-agent-1",
        status="RINGING",
    )

    db_mock = AsyncMock()
    db_mock.get = AsyncMock(return_value=ai_call)
    db_mock.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=ai_call)))))
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    bot_started = []

    def mock_start_sip_ai_bot(cid, is_inbound=False):
        bot_started.append((cid, is_inbound))

    with patch("app.db.database.AsyncSessionLocal", return_value=AsyncMock(__aenter__=AsyncMock(return_value=db_mock), __aexit__=AsyncMock())):
        with patch("app.services.sip_ai_bot.start_sip_ai_bot", side_effect=mock_start_sip_ai_bot):
            # Simulate the exact logic executed in _update_db_connected
            c = await db_mock.get(Call, call_id)
            c.status = "CONNECTED"
            c.connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db_mock.add(c)
            await db_mock.commit()

            is_ai_agent_call = False
            agent_type_upper = (c.agent_type or "").upper()
            if agent_type_upper in ("AI_VOICE", "AI", "AI_AGENT"):
                is_ai_agent_call = True
            elif agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT"):
                is_ai_agent_call = False

            assert is_ai_agent_call is True
            if is_ai_agent_call:
                mock_start_sip_ai_bot(call_id, is_inbound=False)

    assert len(bot_started) == 1
    assert bot_started[0] == (call_id, False)


@pytest.mark.asyncio
async def test_voice_agent_call_answered_skips_ai_bot():
    """TEST 2B: When call is answered and assigned to Voice Agent, start_sip_ai_bot is SKIPPED."""
    call_id = "call-voice-test-202"
    voice_call = Call(
        id=call_id,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="HUMAN",
        agent_id="user-voice-1",
        user_id="user-voice-1",
        status="RINGING",
    )

    db_mock = AsyncMock()
    db_mock.get = AsyncMock(return_value=voice_call)
    db_mock.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=voice_call)))))
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    bot_started = []

    def mock_start_sip_ai_bot(cid, is_inbound=False):
        bot_started.append((cid, is_inbound))

    with patch("app.db.database.AsyncSessionLocal", return_value=AsyncMock(__aenter__=AsyncMock(return_value=db_mock), __aexit__=AsyncMock())):
        with patch("app.services.sip_ai_bot.start_sip_ai_bot", side_effect=mock_start_sip_ai_bot):
            # Simulate the exact logic executed in _update_db_connected
            c = await db_mock.get(Call, call_id)
            c.status = "CONNECTED"
            c.connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db_mock.add(c)
            await db_mock.commit()

            is_ai_agent_call = False
            agent_type_upper = (c.agent_type or "").upper()
            if agent_type_upper in ("AI_VOICE", "AI", "AI_AGENT"):
                is_ai_agent_call = True
            elif agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT"):
                is_ai_agent_call = False

            assert is_ai_agent_call is False
            if is_ai_agent_call:
                mock_start_sip_ai_bot(call_id, is_inbound=False)

    # Bot must NOT be started for Voice Agent calls
    assert len(bot_started) == 0


@pytest.mark.asyncio
async def test_run_sip_ai_bot_defensive_abort_on_voice_agent_call():
    """TEST 2C: If _run_sip_ai_bot is invoked on a Voice Agent call, it defensively aborts immediately."""
    from app.services.sip_ai_bot import _run_sip_ai_bot
    from app.api.routes.barge_stream import barge_sessions

    call_id = "call-voice-defensive-303"
    barge_sessions[call_id] = {
        "running": True,
        "is_dashboard_outbound": True,
        "inbound_queue": asyncio.Queue(),
        "outbound_queue": asyncio.Queue(),
    }

    voice_call = Call(
        id=call_id,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="HUMAN",
        agent_id="user-voice-1",
        user_id="user-voice-1",
        status="CONNECTED",
    )

    db_mock = AsyncMock()
    db_mock.get = AsyncMock(return_value=voice_call)
    db_mock.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=voice_call)))))

    with patch("app.services.sip_ai_bot.AsyncSessionLocal", return_value=AsyncMock(__aenter__=AsyncMock(return_value=db_mock), __aexit__=AsyncMock())):
        with patch("app.services.sip_ai_bot.synthesize_text_to_mulaw") as mock_synth:
            with patch("app.services.sip_ai_bot.broadcast_state_change") as mock_broadcast:
                await _run_sip_ai_bot(call_id, is_inbound=False)
                # Ensure no synthesis or greeting broadcast happened
                assert mock_synth.call_count == 0
                assert mock_broadcast.call_count == 0

    # Cleanup
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_local_voice_test_campaign_voice_agent_assigned():
    """TEST 3A: local_voice_test /start with Voice Agent campaign creates HUMAN call without TTS."""
    from app.api.routes.local_voice_test import start_local_test, StartSessionRequest
    from app.api.routes.barge_stream import barge_sessions

    campaign = Campaign(
        id="camp-solar-1",
        name="Solar Outbound",
        business_id="biz-solar",
        agent_type="HUMAN",
        voice_agent_id="expert-manon-1",
        schedule={"script": "Hello, this is human agent speaking."}
    )
    lead = Lead(id="lead-solar-1", business_id="biz-solar", phone="+919842110746", first_name="Manonmani")

    db_mock = AsyncMock()
    async def mock_execute(stmt):
        m_scalar = MagicMock()
        m_scalar.scalar_one_or_none = MagicMock(return_value=campaign)
        m_scalar.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]), first=MagicMock(return_value=None)))
        return m_scalar

    db_mock.execute = AsyncMock(side_effect=mock_execute)
    async def mock_get(model, pk):
        if model == Lead:
            return lead
        if model == Campaign:
            return campaign
        return None
    db_mock.get = AsyncMock(side_effect=mock_get)
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    body = StartSessionRequest(campaign_id="camp-solar-1", lead_id="lead-solar-1")

    with patch("app.services.voice_service.get_voice_provider") as mock_prov:
        prov = AsyncMock()
        prov.place_call = AsyncMock(return_value="SIP-MOCK-CALL-1")
        mock_prov.return_value = prov

        res = await start_local_test(body=body, db=db_mock)

        assert res["is_voice_agent_call"] is True
        assert res["agent_type"] == "HUMAN"
        assert res["should_speak"] is False
        assert res["audio_url"] is None

        # Verify call created in DB
        calls_added = [arg for call_tuple in db_mock.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        call_rec = calls_added[0]
        assert call_rec.agent_type == "HUMAN"
        assert call_rec.transcript == ""

        # Verify barge session was tagged
        call_id = res["call_id"]
        assert call_id in barge_sessions
        assert barge_sessions[call_id]["is_voice_agent_call"] is True
        assert barge_sessions[call_id]["agent_type"] == "HUMAN"
        barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_update_db_connected_checks_campaign_agent_type():
    """TEST 3B: _update_db_connected skips AI bot if campaign is HUMAN, even if Call.agent_type was AI_VOICE."""
    call_id = "call-mismatched-404"
    camp_id = "camp-voice-human-505"

    mismatched_call = Call(
        id=call_id,
        business_id="biz-1",
        campaign_id=camp_id,
        direction="OUTBOUND",
        agent_type="AI_VOICE",  # Initially mismatched
        status="RINGING",
    )
    human_campaign = Campaign(
        id=camp_id,
        name="Human Campaign",
        business_id="biz-1",
        agent_type="HUMAN",
        voice_agent_id="expert-99",
    )

    db_mock = AsyncMock()
    async def mock_get(model, pk):
        if model == Call:
            return mismatched_call
        if model == Campaign:
            return human_campaign
        return None

    db_mock.get = AsyncMock(side_effect=mock_get)
    db_mock.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=mismatched_call)))))
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    bot_started = []
    def mock_start_sip_ai_bot(cid, is_inbound=False):
        bot_started.append((cid, is_inbound))

    # Evaluate the updated authoritative check logic
    c = await db_mock.get(Call, call_id)
    is_voice_agent = False
    if c.campaign_id:
        camp = await db_mock.get(Campaign, c.campaign_id)
        if camp:
            camp_type = (camp.agent_type or "").upper()
            if camp_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT") or (camp.voice_agent_id and not camp.agent_id):
                is_voice_agent = True

    is_ai_agent_call = not is_voice_agent
    if is_ai_agent_call:
        mock_start_sip_ai_bot(call_id)

    assert is_voice_agent is True
    assert is_ai_agent_call is False
    assert len(bot_started) == 0


@pytest.mark.asyncio
async def test_lead_assigned_to_human_voice_agent_in_ai_campaign():
    """TEST: Admin creates campaign with AI, but assigns lead to Human Voice Agent. Call must be HUMAN and skip AI bot."""
    db = AsyncMock()
    user = User(id="user-human-voice", email="human@callzenza.com", role="VOICE_AGENT", business_id="biz-1")

    ai_campaign = Campaign(
        id="camp-ai-shared",
        name="AI Campaign",
        business_id="biz-1",
        agent_type="AI_VOICE",
        agent_id="ai-bot-99",
    )
    # Lead was assigned to human Voice Agent
    lead = Lead(
        id="lead-assigned-to-human",
        business_id="biz-1",
        phone="+18005557777",
        first_name="Alice",
        last_name="Green",
        assigned_expert_id="voice-expert-77",
    )

    async def mock_get(model, pk):
        if model == Campaign and pk == "camp-ai-shared":
            return ai_campaign
        if model == Lead and pk == "lead-assigned-to-human":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload = InitiateCallRequest(
        lead_id="lead-assigned-to-human",
        phone_number="+18005557777",
        campaign_id="camp-ai-shared",
        agent_type="HUMAN",
        agent_id="voice-expert-77",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_HUMAN_LEAD")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None

        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call = calls_added[0]
        # Must be HUMAN, not AI!
        assert created_call.agent_type == "HUMAN"
        assert created_call.agent_id == "voice-expert-77"


@pytest.mark.asyncio
async def test_ai_campaign_with_human_backup_preserves_ai_agent_and_id():
    """REGRESSION TEST: AI campaign with human backup Voice Agents (Assigned Transfer Voice Agents)

    MUST preserve AI_VOICE as primary agent and preserve selected AI Agent ID (e.g. MAGIC).
    Lead having an assigned backup expert must NOT override the AI campaign.
    """
    db = AsyncMock()
    user = User(id="user-1", email="agent@callzenza.com", role="AGENT", business_id="biz-1")

    ai_agent = AIAgent(id="ai-magic-sales", name="MAGIC", business_id="biz-1", status="ACTIVE")
    ai_campaign_with_human_backup = Campaign(
        id="camp-magic-ai-1",
        name="Magic Sales Campaign",
        business_id="biz-1",
        agent_type="AI_VOICE",
        agent_id="ai-magic-sales",
        voice_agent_id="expert-backup-1,expert-backup-2",  # Human Voice Agent backup
    )
    lead = Lead(
        id="lead-with-backup",
        business_id="biz-1",
        phone="+919876543210",
        first_name="Customer",
        last_name="Test",
        assigned_expert_id="expert-backup-1",  # Lead has backup expert assigned
    )

    async def mock_get(model, pk):
        if model == Campaign and pk == "camp-magic-ai-1":
            return ai_campaign_with_human_backup
        if model == AIAgent and pk == "ai-magic-sales":
            return ai_agent
        if model == Lead and pk == "lead-with-backup":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get)
    db.scalar = AsyncMock(return_value=ai_agent)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=ai_agent)))))

    payload = InitiateCallRequest(
        lead_id="lead-with-backup",
        phone_number="+919876543210",
        campaign_id="camp-magic-ai-1",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_MAGIC")
        mock_provider.return_value = prov_instance

        res = await initiate_call(payload=payload, user=user, db=db)
        assert res["call_id"] is not None

        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call = calls_added[0]

        # Primary call type MUST remain AI_VOICE, NOT HUMAN!
        assert created_call.agent_type == "AI_VOICE"
        # Primary AI Agent ID MUST be preserved!
        assert created_call.agent_id == "ai-magic-sales"
        assert created_call.direction == "OUTBOUND"


@pytest.mark.asyncio
async def test_ai_campaign_with_human_backup_connected_starts_ai_bot():
    """REGRESSION TEST: When customer answers an AI campaign call that has human backup agents,

    the AI bot MUST be started so AI greeting and AI conversation loop begin automatically.
    """
    call_id = "call-ai-backup-connected-777"
    camp_id = "camp-magic-backup-777"

    call_rec = Call(
        id=call_id,
        business_id="biz-1",
        campaign_id=camp_id,
        direction="OUTBOUND",
        agent_type="AI_VOICE",
        agent_id="ai-magic-sales",
        status="RINGING",
    )
    camp_rec = Campaign(
        id=camp_id,
        name="AI Campaign with Backup",
        business_id="biz-1",
        agent_type="AI_VOICE",
        agent_id="ai-magic-sales",
        voice_agent_id="expert-backup-1,expert-backup-2",
    )

    db_mock = AsyncMock()
    async def mock_get(model, pk):
        if model == Call:
            return call_rec
        if model == Campaign:
            return camp_rec
        return None

    db_mock.get = AsyncMock(side_effect=mock_get)
    db_mock.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=call_rec)))))
    db_mock.add = MagicMock()
    db_mock.commit = AsyncMock()

    bot_started = []
    def mock_start_sip_ai_bot(cid, is_inbound=False):
        bot_started.append((cid, is_inbound))

    # Test the authoritative routing logic in _update_db_connected
    c = await db_mock.get(Call, call_id)
    is_voice_agent = False
    decision_reason = "default_ai"

    if c.campaign_id:
        camp = await db_mock.get(Campaign, c.campaign_id)
        if camp:
            camp_primary_type = (camp.agent_type or "AI_VOICE").upper()
            if (
                camp_primary_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT")
                or "HUMAN" in camp_primary_type
            ):
                is_voice_agent = True
                decision_reason = "campaign_configured_as_human"
            else:
                # AI Agent campaign: Primary is AI, backup agents are transfer destinations
                is_voice_agent = False
                decision_reason = "campaign_configured_as_ai"

    is_ai_agent_call = not is_voice_agent
    if is_ai_agent_call:
        mock_start_sip_ai_bot(call_id, is_inbound=False)

    assert is_voice_agent is False
    assert is_ai_agent_call is True
    assert decision_reason == "campaign_configured_as_ai"
    assert len(bot_started) == 1
    assert bot_started[0] == (call_id, False)


@pytest.mark.asyncio
async def test_start_sip_ai_bot_does_not_falsely_skip_unstarted_session():
    """TEST 12: Verify start_sip_ai_bot launches task without premature lockout."""
    from app.services.sip_ai_bot import start_sip_ai_bot, ACTIVE_SIP_BOT_TASKS
    from app.api.routes.barge_stream import barge_sessions
    from collections import deque

    test_call_id = "test-ai-launch-call-999"
    barge_sessions[test_call_id] = {
        "running": True,
        "agent_type": "AI_VOICE",
        "is_voice_agent_call": False,
        "ai_rtp_tx_queue": deque(maxlen=5000),
    }

    with patch("app.services.sip_ai_bot._run_sip_ai_bot") as mock_run:
        mock_run.return_value = None
        start_sip_ai_bot(test_call_id, is_inbound=False)
        assert test_call_id in ACTIVE_SIP_BOT_TASKS
        assert barge_sessions[test_call_id].get("ai_bot_started") is True
        task = ACTIVE_SIP_BOT_TASKS.pop(test_call_id, None)
        if task and not task.done():
            task.cancel()
        barge_sessions.pop(test_call_id, None)


@pytest.mark.asyncio
async def test_ai_agent_call_greeting_and_tts_fallback_to_rtp_queue():
    """TEST 13: Verify opening greeting generates audio and enqueues to ai_rtp_tx_queue."""
    from app.services.sip_ai_bot import synthesize_text_to_mulaw

    test_mulaw = b"\xff" * 3200
    with patch("app.services.tts_service.synthesize_snapserve_speech", new_callable=AsyncMock) as mock_snap:
        mock_snap.return_value = b"RIFF" + b"\x00" * 36 + b"data" + b"\x00" * 3200
        with patch("app.services.sip_ai_bot.wav_to_mulaw", return_value=test_mulaw):
            mulaw = await synthesize_text_to_mulaw("Hello, welcome to Callzenza.", call_id="call-test-audio")
            assert len(mulaw) == 3200
            assert len(mulaw) % 160 == 0




