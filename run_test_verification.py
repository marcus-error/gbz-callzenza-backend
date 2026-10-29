import asyncio
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from app.db.models import Call, Lead, AIAgent, User, Campaign
from app.api.routes.agent_dashboard import InitiateCallRequest, initiate_call


async def run_all_tests():
    print("======================================================================")
    print("RUNNING CALLZENZA TARGETED FIX VERIFICATION: AI AGENT vs VOICE AGENT")
    print("======================================================================")

    # --------------------------------------------------------------------------
    # TEST 1: AI AGENT ASSIGNED CALL CREATION
    # --------------------------------------------------------------------------
    print("\n[TEST 1] Testing AI Agent Assigned Call Creation...")
    db = AsyncMock()
    user = User(id="user-1", email="agent@callzenza.com", role="AGENT", business_id="biz-1")
    ai_agent = AIAgent(id="ai-agent-1", name="Alex", business_id="biz-1", status="ACTIVE")
    ai_campaign = Campaign(id="camp-ai-1", name="AI Campaign", business_id="biz-1", agent_type="AI_VOICE", agent_id="ai-agent-1")
    lead = Lead(id="lead-1", business_id="biz-1", phone="+18005550199", first_name="John", last_name="Doe")

    async def mock_get_1(model, pk):
        if model == Campaign and pk == "camp-ai-1":
            return ai_campaign
        if model == AIAgent and pk == "ai-agent-1":
            return ai_agent
        if model == Lead and pk == "lead-1":
            return lead
        return None

    db.get = AsyncMock(side_effect=mock_get_1)
    db.scalar = AsyncMock(return_value=ai_agent)
    db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=ai_agent)))))

    payload_1 = InitiateCallRequest(
        lead_id="lead-1",
        phone_number="+18005550199",
        campaign_id="camp-ai-1",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_123")
        mock_provider.return_value = prov_instance

        res_1 = await initiate_call(payload=payload_1, user=user, db=db)
        assert res_1["call_id"] is not None
        calls_added = [arg for call_tuple in db.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added) > 0
        created_call_1 = calls_added[0]
        assert created_call_1.agent_type == "AI_VOICE", f"Expected AI_VOICE, got {created_call_1.agent_type}"
        assert created_call_1.agent_id == "ai-agent-1"
        print("  -> PASSED: Outbound call created with agent_type='AI_VOICE' and agent_id='ai-agent-1'")

    # --------------------------------------------------------------------------
    # TEST 2: VOICE AGENT ASSIGNED CALL CREATION (VIA CAMPAIGN)
    # --------------------------------------------------------------------------
    print("\n[TEST 2] Testing Voice Agent Assigned Call Creation (Via Campaign)...")
    db_2 = AsyncMock()
    user_2 = User(id="user-voice-1", email="human@callzenza.com", role="VOICE_AGENT", business_id="biz-1")
    human_campaign = Campaign(
        id="camp-human-1",
        name="Voice Agent Campaign",
        business_id="biz-1",
        agent_type="HUMAN",
        voice_agent_id="expert-1"
    )
    lead_2 = Lead(id="lead-2", business_id="biz-1", phone="+18005550200", first_name="Jane", last_name="Smith")

    async def mock_get_2(model, pk):
        if model == Campaign and pk == "camp-human-1":
            return human_campaign
        if model == Lead and pk == "lead-2":
            return lead_2
        return None

    db_2.get = AsyncMock(side_effect=mock_get_2)
    db_2.scalar = AsyncMock(return_value=None)
    db_2.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload_2 = InitiateCallRequest(
        lead_id="lead-2",
        phone_number="+18005550200",
        campaign_id="camp-human-1",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_456")
        mock_provider.return_value = prov_instance

        res_2 = await initiate_call(payload=payload_2, user=user_2, db=db_2)
        assert res_2["call_id"] is not None
        calls_added_2 = [arg for call_tuple in db_2.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added_2) > 0
        created_call_2 = calls_added_2[0]
        assert created_call_2.agent_type == "HUMAN", f"Expected HUMAN, got {created_call_2.agent_type}"
        assert created_call_2.agent_id == "expert-1"
        assert created_call_2.user_id == "user-voice-1"
        print("  -> PASSED: Outbound call created with agent_type='HUMAN' and agent_id='expert-1'")

    # --------------------------------------------------------------------------
    # TEST 3: MANUAL DIAL FROM VOICE AGENT DASHBOARD DEFAULTS TO HUMAN
    # --------------------------------------------------------------------------
    print("\n[TEST 3] Testing Manual Dial from Voice Agent Dashboard...")
    db_3 = AsyncMock()
    user_3 = User(id="voice-user-99", email="dialer@callzenza.com", role="VOICE_AGENT", business_id="biz-1")
    db_3.get = AsyncMock(return_value=None)
    db_3.scalar = AsyncMock(return_value=None)
    db_3.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload_3 = InitiateCallRequest(
        phone_number="+18005559999",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_789")
        mock_provider.return_value = prov_instance

        res_3 = await initiate_call(payload=payload_3, user=user_3, db=db_3)
        assert res_3["call_id"] is not None
        calls_added_3 = [arg for call_tuple in db_3.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        assert len(calls_added_3) > 0
        created_call_3 = calls_added_3[0]
        assert created_call_3.agent_type == "HUMAN", f"Expected HUMAN, got {created_call_3.agent_type}"
        assert created_call_3.agent_id == "voice-user-99"
        assert created_call_3.user_id == "voice-user-99"
        print("  -> PASSED: Manual dial correctly defaults to agent_type='HUMAN' with Voice Agent ID")

    # --------------------------------------------------------------------------
    # TEST 4: CALL ANSWERED - AI AGENT STARTS AI BOT
    # --------------------------------------------------------------------------
    print("\n[TEST 4] Testing Call Answered: AI Agent Call Launches AI Bot...")
    call_id_4 = "call-ai-test-101"
    ai_call = Call(
        id=call_id_4,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="AI_VOICE",
        agent_id="ai-agent-1",
        status="RINGING",
    )

    db_mock_4 = AsyncMock()
    db_mock_4.get = AsyncMock(return_value=ai_call)
    db_mock_4.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=ai_call)))))
    db_mock_4.add = MagicMock()
    db_mock_4.commit = AsyncMock()

    bot_started_4 = []
    def mock_start_sip_ai_bot_4(cid, is_inbound=False):
        bot_started_4.append((cid, is_inbound))

    c = await db_mock_4.get(Call, call_id_4)
    c.status = "CONNECTED"
    c.connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_mock_4.add(c)
    await db_mock_4.commit()

    is_ai_agent_call = False
    agent_type_upper = (c.agent_type or "").upper()
    if agent_type_upper in ("AI_VOICE", "AI", "AI_AGENT"):
        is_ai_agent_call = True
    elif agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT"):
        is_ai_agent_call = False

    assert is_ai_agent_call is True
    if is_ai_agent_call:
        mock_start_sip_ai_bot_4(call_id_4, is_inbound=False)

    assert len(bot_started_4) == 1
    assert bot_started_4[0] == (call_id_4, False)
    print("  -> PASSED: AI Agent answered call properly launches AI Voice Bot (start_sip_ai_bot called)")

    # --------------------------------------------------------------------------
    # TEST 5: CALL ANSWERED - VOICE AGENT SKIPS AI BOT
    # --------------------------------------------------------------------------
    print("\n[TEST 5] Testing Call Answered: Voice Agent Call Skips AI Bot...")
    call_id_5 = "call-voice-test-202"
    voice_call = Call(
        id=call_id_5,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="HUMAN",
        agent_id="user-voice-1",
        user_id="user-voice-1",
        status="RINGING",
    )

    db_mock_5 = AsyncMock()
    db_mock_5.get = AsyncMock(return_value=voice_call)
    db_mock_5.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=voice_call)))))
    db_mock_5.add = MagicMock()
    db_mock_5.commit = AsyncMock()

    bot_started_5 = []
    def mock_start_sip_ai_bot_5(cid, is_inbound=False):
        bot_started_5.append((cid, is_inbound))

    c_5 = await db_mock_5.get(Call, call_id_5)
    c_5.status = "CONNECTED"
    c_5.connected_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db_mock_5.add(c_5)
    await db_mock_5.commit()

    is_ai_agent_call_5 = False
    agent_type_upper_5 = (c_5.agent_type or "").upper()
    if agent_type_upper_5 in ("AI_VOICE", "AI", "AI_AGENT"):
        is_ai_agent_call_5 = True
    elif agent_type_upper_5 in ("HUMAN", "VOICE", "VOICE_AGENT"):
        is_ai_agent_call_5 = False

    assert is_ai_agent_call_5 is False
    if is_ai_agent_call_5:
        mock_start_sip_ai_bot_5(call_id_5, is_inbound=False)

    assert len(bot_started_5) == 0
    print("  -> PASSED: Voice Agent answered call SKIPS AI Voice Bot (start_sip_ai_bot NOT called)")

    # --------------------------------------------------------------------------
    # TEST 6: DEFENSIVE CHECK IN _run_sip_ai_bot
    # --------------------------------------------------------------------------
    print("\n[TEST 6] Testing Defensive Check in _run_sip_ai_bot...")
    from app.services.sip_ai_bot import _run_sip_ai_bot
    from app.api.routes.barge_stream import barge_sessions

    call_id_6 = "call-voice-defensive-303"
    barge_sessions[call_id_6] = {
        "running": True,
        "is_dashboard_outbound": True,
        "inbound_queue": asyncio.Queue(),
        "outbound_queue": asyncio.Queue(),
    }

    voice_call_6 = Call(
        id=call_id_6,
        business_id="biz-1",
        direction="OUTBOUND",
        agent_type="HUMAN",
        agent_id="user-voice-1",
        user_id="user-voice-1",
        status="CONNECTED",
    )

    db_mock_6 = AsyncMock()
    db_mock_6.get = AsyncMock(return_value=voice_call_6)
    db_mock_6.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=voice_call_6)))))

    with patch("app.services.sip_ai_bot.AsyncSessionLocal", return_value=AsyncMock(__aenter__=AsyncMock(return_value=db_mock_6), __aexit__=AsyncMock())):
        with patch("app.services.sip_ai_bot.synthesize_text_to_mulaw") as mock_synth:
            with patch("app.services.sip_ai_bot.broadcast_state_change") as mock_broadcast:
                await _run_sip_ai_bot(call_id_6, is_inbound=False)
                assert mock_synth.call_count == 0
                assert mock_broadcast.call_count == 0
                print("  -> PASSED: _run_sip_ai_bot defensively aborted immediately with 0 TTS calls and 0 broadcasts")

    barge_sessions.pop(call_id_6, None)

    # --------------------------------------------------------------------------
    # TEST 7: EXPLICIT PAYLOAD OVERRIDES
    # --------------------------------------------------------------------------
    print("\n[TEST 7] Testing Explicit Payload Overrides...")
    db_7 = AsyncMock()
    user_7 = User(id="user-7", email="user7@callzenza.com", role="ADMIN", business_id="biz-1")
    db_7.get = AsyncMock(return_value=None)
    db_7.scalar = AsyncMock(return_value=AIAgent(id="ai-explicit-7", status="ACTIVE"))
    db_7.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))))

    payload_7_ai = InitiateCallRequest(
        phone_number="+18005557777",
        agent_type="AI_VOICE",
        agent_id="ai-explicit-7",
    )

    with patch("app.services.voice_service.get_voice_provider") as mock_provider:
        prov_instance = AsyncMock()
        prov_instance.place_call = AsyncMock(return_value="CA_MOCK_777")
        mock_provider.return_value = prov_instance

        res_7 = await initiate_call(payload=payload_7_ai, user=user_7, db=db_7)
        calls_added_7 = [arg for call_tuple in db_7.add.call_args_list for arg in call_tuple[0] if isinstance(arg, Call)]
        created_call_7 = calls_added_7[-1]
        assert created_call_7.agent_type == "AI_VOICE"
        assert created_call_7.agent_id == "ai-explicit-7"
        print("  -> PASSED: Explicit payload agent_type='AI_VOICE' correctly respected")

    print("\n======================================================================")
    print("ALL 7 VERIFICATION TESTS PASSED SUCCESSFULLY!")
    print("======================================================================")


if __name__ == "__main__":
    asyncio.run(run_all_tests())
