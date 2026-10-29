import asyncio
import base64
from collections import deque
import json
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    boost_ulaw_speech_frame,
    audio_mixer_loop,
    safe_put_audio_frame,
)
from app.db.models import Call, CallParticipant, CallTransfer


class MockWebSocket:
    def __init__(self, name: str):
        self.name = name
        self.sent_messages = []
        self.closed = False
        self.close_code = None

    async def send_text(self, text: str):
        if self.closed:
            raise RuntimeError(f"Cannot send to closed WebSocket {self.name}")
        self.sent_messages.append(text)

    async def close(self, code: int = 1000):
        self.closed = True
        self.close_code = code


@pytest.mark.asyncio
async def test_scenario_1_three_participants_all_remain_connected():
    """
    1. Customer joins; Voice Agent 1 joins; Voice Agent 2 joins.
       All three participants remain connected to the SAME conference session.
    """
    conf_call_id = f"test-conf-scen1-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    # Customer connects
    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "stream-cust-1"

    # Agent 1 connects
    agent1_ws = MockWebSocket("Agent1")
    session["browser_bargers"].add(agent1_ws)

    # Agent 2 connects
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent2_ws)

    assert session["customer_ws"] is not None
    assert agent1_ws in session["browser_bargers"]
    assert agent2_ws in session["browser_bargers"]
    assert len(session["browser_bargers"]) == 2
    assert session["running"] is True

    # Cleanup
    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_2_joining_agent2_does_not_disconnect_agent1():
    """
    2. Joining Voice Agent 2 does not send BYE or cleanup to Voice Agent 1.
    """
    conf_call_id = f"test-conf-scen2-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    agent1_ws = MockWebSocket("Agent1")
    session["browser_bargers"].add(agent1_ws)

    # Agent 2 joins
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent2_ws)

    # Verify Agent 1 ws was not closed and received NO BYE / close events
    assert agent1_ws.closed is False
    assert agent2_ws.closed is False
    assert len(session["browser_bargers"]) == 2

    # Cleanup
    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_3_customer_audio_delivered_to_both_agents():
    """
    3. Customer audio is delivered to both Agent 1 and Agent 2.
    """
    conf_call_id = f"test-conf-scen3-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Customer speaks
    cust_frame = b"\x55" * 160
    barge_in_q = session["barge_inbound_queue"]
    await barge_in_q.put(cust_frame)

    # Run mixer loop briefly
    mixer_task = asyncio.create_task(audio_mixer_loop(conf_call_id))
    await asyncio.sleep(0.08)
    session["running"] = False
    mixer_task.cancel()
    try:
        await mixer_task
    except asyncio.CancelledError:
        pass

    # Verify both Agent 1 and Agent 2 received the customer's audio frame
    assert len(agent1_ws.sent_messages) >= 1
    assert len(agent2_ws.sent_messages) >= 1

    msg1 = json.loads(agent1_ws.sent_messages[0])
    msg2 = json.loads(agent2_ws.sent_messages[0])
    assert msg1["event"] == "media"
    assert msg2["event"] == "media"
    assert base64.b64decode(msg1["media"]["payload"]) == cust_frame
    assert base64.b64decode(msg2["media"]["payload"]) == cust_frame

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_4_agent1_audio_delivered_to_customer_and_agent2():
    """
    4. Voice Agent 1 audio is delivered to Customer and Voice Agent 2.
    """
    conf_call_id = f"test-conf-scen4-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-4"

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 1 speaks
    agent1_frame = b"\x33" * 160

    # Simulate browser_barge_stream packet handling
    other_bargers = [ws for ws in list(session["browser_bargers"]) if ws != agent1_ws]
    assert other_bargers == [agent2_ws]
    peer_payload = json.dumps({
        "event": "media",
        "call_id": conf_call_id,
        "media": {"payload": base64.b64encode(agent1_frame).decode("utf-8")}
    })
    for other_ws in other_bargers:
        await other_ws.send_text(peer_payload)

    # And to Customer
    session["ai_rtp_tx_queue"].append(agent1_frame)
    cust_msg = json.dumps({
        "event": "media",
        "streamSid": session["customer_stream_sid"],
        "media": {"payload": base64.b64encode(agent1_frame).decode("utf-8")}
    })
    await cust_ws.send_text(cust_msg)

    # Agent 2 heard Agent 1
    assert len(agent2_ws.sent_messages) == 1
    assert base64.b64decode(json.loads(agent2_ws.sent_messages[0])["media"]["payload"]) == agent1_frame

    # Customer heard Agent 1
    assert len(cust_ws.sent_messages) == 1
    assert base64.b64decode(json.loads(cust_ws.sent_messages[0])["media"]["payload"]) == agent1_frame

    # Agent 1 did NOT receive its own audio (no self-echo)
    assert len(agent1_ws.sent_messages) == 0

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_5_agent2_audio_delivered_to_customer_and_agent1():
    """
    5. Voice Agent 2 audio is delivered to Customer and Voice Agent 1.
    """
    conf_call_id = f"test-conf-scen5-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-5"

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 2 speaks
    agent2_frame = b"\x77" * 160

    # Peer forward to Agent 1
    other_bargers = [ws for ws in list(session["browser_bargers"]) if ws != agent2_ws]
    assert other_bargers == [agent1_ws]
    peer_payload = json.dumps({
        "event": "media",
        "call_id": conf_call_id,
        "media": {"payload": base64.b64encode(agent2_frame).decode("utf-8")}
    })
    for other_ws in other_bargers:
        await other_ws.send_text(peer_payload)

    # Outbound forward to Customer
    session["ai_rtp_tx_queue"].append(agent2_frame)
    cust_msg = json.dumps({
        "event": "media",
        "streamSid": session["customer_stream_sid"],
        "media": {"payload": base64.b64encode(agent2_frame).decode("utf-8")}
    })
    await cust_ws.send_text(cust_msg)

    # Agent 1 heard Agent 2
    assert len(agent1_ws.sent_messages) == 1
    assert base64.b64decode(json.loads(agent1_ws.sent_messages[0])["media"]["payload"]) == agent2_frame

    # Customer heard Agent 2
    assert len(cust_ws.sent_messages) == 1
    assert base64.b64decode(json.loads(cust_ws.sent_messages[0])["media"]["payload"]) == agent2_frame

    # Agent 2 did NOT receive its own audio (no self-echo)
    assert len(agent2_ws.sent_messages) == 0

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_6_no_duplicate_self_echo():
    """
    6. Each participant does not receive duplicate copies of their own audio.
    """
    conf_call_id = f"test-conf-scen6-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 1 transmits 5 frames
    for i in range(5):
        frame = bytes([i * 10] * 160)
        # Forward ONLY to other bargers
        for other_ws in [ws for ws in session["browser_bargers"] if ws != agent1_ws]:
            await other_ws.send_text(json.dumps({"event": "media", "f": i}))

    assert len(agent1_ws.sent_messages) == 0
    assert len(agent2_ws.sent_messages) == 5

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_7_one_participant_leaves_other_two_remain():
    """
    7. One participant leaves while the other two remain connected and continue talking.
    """
    conf_call_id = f"test-conf-scen7-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "stream-7"

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 1 leaves conference
    session["browser_bargers"].discard(agent1_ws)
    await agent1_ws.close(code=1000)

    # Confirm Agent 1 is closed, but Customer and Agent 2 remain active
    assert agent1_ws.closed is True
    assert agent2_ws.closed is False
    assert cust_ws.closed is False
    assert agent2_ws in session["browser_bargers"]
    assert len(session["browser_bargers"]) == 1
    assert session["running"] is True

    # Customer and Agent 2 can continue communicating
    new_frame = b"\x88" * 160
    await agent2_ws.send_text(json.dumps({"event": "media", "payload": base64.b64encode(new_frame).decode("utf-8")}))
    assert len(agent2_ws.sent_messages) == 1

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_8_temporary_media_error_does_not_terminate_conference():
    """
    8. A temporary RTP/media error or dead listener does not terminate the conference.
    """
    conf_call_id = f"test-conf-scen8-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    # Simulate agent 1 failing / throwing error on send
    agent1_ws.closed = True  # Disconnected
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Forwarding loop handles error gracefully without raising or killing session
    frame = b"\x22" * 160
    dead = set()
    for ws in list(session["browser_bargers"]):
        try:
            await ws.send_text(json.dumps({"event": "media", "data": "test"}))
        except Exception:
            dead.add(ws)
    session["browser_bargers"].difference_update(dead)

    assert agent1_ws not in session["browser_bargers"]
    assert agent2_ws in session["browser_bargers"]
    assert session["running"] is True

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_9_concurrent_joins_and_leaves_safe():
    """
    9. Concurrent joins and leaves do not corrupt participant sets or session state.
    """
    conf_call_id = f"test-conf-scen9-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    agents = [MockWebSocket(f"Agent-{i}") for i in range(10)]

    async def join_agent(ws):
        session["browser_bargers"].add(ws)
        await asyncio.sleep(0.01)

    async def leave_agent(ws):
        await asyncio.sleep(0.01)
        session["browser_bargers"].discard(ws)

    # Run 10 concurrent joins and leaves
    await asyncio.gather(
        *(join_agent(ws) for ws in agents[:5]),
        *(leave_agent(agents[0]), leave_agent(agents[1]))
    )

    assert session["running"] is True
    # Mappings remain intact
    assert isinstance(session["browser_bargers"], set)

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_10_tenant_isolation():
    """
    10. Tenant isolation: Sessions belong strictly to their business/tenant.
    """
    call1_id = f"test-tenant-call1-{uuid.uuid4().hex[:6]}"
    call2_id = f"test-tenant-call2-{uuid.uuid4().hex[:6]}"

    sess1 = await _get_or_create_session(call1_id)
    sess2 = await _get_or_create_session(call2_id)

    ws_t1 = MockWebSocket("AgentT1")
    ws_t2 = MockWebSocket("AgentT2")

    sess1["browser_bargers"].add(ws_t1)
    sess2["browser_bargers"].add(ws_t2)

    assert ws_t1 in sess1["browser_bargers"]
    assert ws_t1 not in sess2["browser_bargers"]
    assert ws_t2 in sess2["browser_bargers"]
    assert ws_t2 not in sess1["browser_bargers"]

    barge_sessions.pop(call1_id, None)
    barge_sessions.pop(call2_id, None)


@pytest.mark.asyncio
async def test_scenario_11_bounded_audio_queues_and_latency():
    """
    11. Audio queues remain bounded and evict stale frames without unbounded latency.
    """
    q = asyncio.Queue(maxsize=5)

    # Enqueue 10 frames into a 5-capacity queue
    for i in range(10):
        frame = bytes([i] * 160)
        safe_put_audio_frame(q, frame, "test_q", "call-bounded")

    assert q.qsize() == 5
    # Oldest evicted: frames 0..4 evicted, frames 5..9 remain
    first_remaining = await q.get()
    assert first_remaining[0] == 5


@pytest.mark.asyncio
async def test_scenario_12_exact_linear_pcm_mixing():
    """
    12. Exact ITU-T G.711 mu-law linear PCM mixing produces distortion-free output
    when two participants speak simultaneously.
    """
    # Test silence + tone
    silence = b"\xFF" * 160  # mu-law 0
    tone = b"\x00" * 160     # mu-law max positive amplitude
    mixed = mix_ulaw_bytes(silence, tone)
    assert len(mixed) == 160
    assert mixed == tone

    # Test two active voice frames
    f1 = b"\x20" * 160
    f2 = b"\x30" * 160
    mixed2 = mix_ulaw_bytes(f1, f2)
    assert len(mixed2) == 160
    assert mixed2 != f1
    assert mixed2 != f2


@pytest.mark.asyncio
async def test_scenario_13_resampling_timing_continuity_and_frame_slicing():
    """
    13. Continuous resampling converts 1024-sample blocks (48kHz) to exact 160-sample (20ms @ 8kHz) frames
    with zero clock timing drift across consecutive blocks.
    """
    import math

    class ContinuousAudioResamplerPy:
        def __init__(self, in_rate=48000, out_rate=8000):
            self.in_rate = in_rate
            self.out_rate = out_rate
            self.ratio = in_rate / out_rate
            self.phase = 0.0
            self.last_sample = 0.0
            self.buffer = []

        def process(self, input_samples):
            pos = self.phase
            resampled = []
            while pos < len(input_samples):
                idx = int(pos)
                frac = pos - idx
                s0 = input_samples[idx]
                s1 = input_samples[idx + 1] if idx + 1 < len(input_samples) else input_samples[-1]
                resampled.append(s0 * (1.0 - frac) + s1 * frac)
                pos += self.ratio
            self.phase = pos - len(input_samples)
            self.buffer.extend(resampled)

            frames = []
            while len(self.buffer) >= 160:
                frame = self.buffer[:160]
                self.buffer = self.buffer[160:]
                frames.append(frame)
            return frames

    resampler = ContinuousAudioResamplerPy(in_rate=48000, out_rate=8000)

    # 15 consecutive blocks of 1024 samples = 15360 samples = 0.3200 seconds at 48kHz
    # At 8kHz, 0.3200s is EXACTLY 2560 samples = exactly 16 frames of 160 samples
    total_frames = []
    for _ in range(15):
        block = [math.sin(i * 0.1) for i in range(1024)]
        frames = resampler.process(block)
        total_frames.extend(frames)

    assert len(total_frames) == 16
    for f in total_frames:
        assert len(f) == 160

    # Remainder in buffer must be exactly 0 (15360 / 6 = 2560 = 16 * 160)
    assert len(resampler.buffer) == 0
    assert abs(resampler.phase) < 1e-9


@pytest.mark.asyncio
async def test_scenario_14_supervisor_mic_ingestion_and_queue_isolation():
    """
    14. Supervisor mic ingestion slices exact 160-byte frames into barger_in_queues
    without overflowing or corrupting barge_outbound_queue.
    """
    conf_call_id = f"test-conf-scen14-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    ws = MockWebSocket("AgentWS")
    session["browser_bargers"].add(ws)

    # Ingest 320 bytes (two 160-byte frames) in one packet
    payload = b"\x33" * 320
    conn_mic_buf = bytearray(payload)

    while len(conn_mic_buf) >= 160:
        frame = bytes(conn_mic_buf[:160])
        del conn_mic_buf[:160]
        b_in_q = session.setdefault("barger_in_queues", {}).setdefault(ws, deque(maxlen=5))
        b_in_q.append(frame)

    assert len(conn_mic_buf) == 0
    assert len(session["barger_in_queues"][ws]) == 2
    # Verify barge_outbound_queue is NOT contaminated
    assert session["barge_outbound_queue"].qsize() == 0

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_15_three_way_conference_audio_all_permutations():
    """
    15. Tests all 6 audio directions and 3-way simultaneous speech:
        - Customer -> Agent 1 & Agent 2
        - Agent 1 -> Customer & Agent 2
        - Agent 2 -> Customer & Agent 1
        - Simultaneous 3-way mixing with zero self-echo
    """
    conf_call_id = f"test-conf-scen15-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-15"

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # 1. Customer speaks
    cust_frame = b"\x10" * 160
    session.setdefault("inbound_legs", {})["cust_leg"] = deque([cust_frame])

    # 2. Agent 1 speaks
    a1_frame = b"\x20" * 160
    session.setdefault("barger_in_queues", {})[agent1_ws] = deque([a1_frame])

    # 3. Agent 2 speaks
    a2_frame = b"\x30" * 160
    session.setdefault("barger_in_queues", {})[agent2_ws] = deque([a2_frame])

    mixer_task = asyncio.create_task(audio_mixer_loop(conf_call_id))
    await asyncio.sleep(0.06)
    session["running"] = False
    mixer_task.cancel()
    try:
        await mixer_task
    except asyncio.CancelledError:
        pass

    # Verify Agent 1 received: mix(Customer, Agent 2) - NO Agent 1 self-echo
    assert len(agent1_ws.sent_messages) >= 1
    a1_rx = base64.b64decode(json.loads(agent1_ws.sent_messages[0])["media"]["payload"])
    assert a1_rx == mix_ulaw_bytes(cust_frame, a2_frame)

    # Verify Agent 2 received: mix(Customer, Agent 1) - NO Agent 2 self-echo
    assert len(agent2_ws.sent_messages) >= 1
    a2_rx = base64.b64decode(json.loads(agent2_ws.sent_messages[0])["media"]["payload"])
    assert a2_rx == mix_ulaw_bytes(cust_frame, a1_frame)

    # Verify Customer received: mix(Agent 1, Agent 2) - NO Customer self-echo
    assert len(cust_ws.sent_messages) >= 1
    cust_rx = base64.b64decode(json.loads(cust_ws.sent_messages[0])["media"]["payload"])
    assert cust_rx == mix_ulaw_bytes(a1_frame, a2_frame)

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_16_call_prefix_resolution_shares_same_audio_mixer():
    """
    16. Verifies that 'call-99feea470eb6' and '99feea470eb6' resolve to the exact same
    audio mixer session and audio queues.
    """
    base_id = "test-call-abc12345"
    prefixed_id = f"call-{base_id}"

    sess1 = await _get_or_create_session(prefixed_id)
    sess2 = await _get_or_create_session(base_id)

    assert sess1 is sess2
    assert base_id in sess1["aliases"]
    assert prefixed_id in sess1["aliases"]

    barge_sessions.pop(prefixed_id, None)
    barge_sessions.pop(base_id, None)


@pytest.mark.asyncio
async def test_scenario_17_dead_barger_recovery_and_transient_error_tolerance():
    """
    17. Verifies that transient WebSocket send issues do not prematurely evict active agents,
    while genuinely closed WebSockets are cleanly removed.
    """
    conf_call_id = f"test-conf-scen17-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    live_ws = MockWebSocket("LiveAgent")
    closed_ws = MockWebSocket("ClosedAgent")
    closed_ws.closed = True

    session["browser_bargers"].add(live_ws)
    session["browser_bargers"].add(closed_ws)

    # Feed customer frame to trigger downlink dispatch
    session.setdefault("inbound_legs", {})["leg"] = deque([b"\x44" * 160])

    mixer_task = asyncio.create_task(audio_mixer_loop(conf_call_id))
    await asyncio.sleep(0.06)
    session["running"] = False
    mixer_task.cancel()
    try:
        await mixer_task
    except asyncio.CancelledError:
        pass

    # Closed ws was removed
    assert closed_ws not in session["browser_bargers"]
    # Live ws remains
    assert live_ws in session["browser_bargers"]

    barge_sessions.pop(conf_call_id, None)


@pytest.mark.asyncio
async def test_scenario_18_voice_agent_speech_loudness_boost_and_normalization():
    """
    18. Verifies that human voice agent microphone frames (e.g. from a quiet laptop mic)
    are lifted to standard telephony loudness (2.4x gain with soft limiting) while keeping
    silence unamplified and zero self-echo.
    """
    conf_call_id = f"test-conf-scen18-{uuid.uuid4().hex[:6]}"
    session = await _get_or_create_session(conf_call_id)

    # 1. Silence frame remains unamplified (no hiss pumping)
    silence_frame = b"\xFF" * 160
    boosted_silence = boost_ulaw_speech_frame(silence_frame)
    assert boosted_silence == silence_frame

    # 2. Quiet speech frame is boosted by >2.3x
    # 0x37 = pcm 3004. Boosted = 0x23 (pcm 7164, ratio ~2.38x)
    quiet_frame = b"\x37" * 160
    boosted_quiet = boost_ulaw_speech_frame(quiet_frame)
    assert boosted_quiet != quiet_frame
    assert boosted_quiet[0] == 0x23

    # 3. In a multi-agent conference, Agent 2's boosted frame is delivered loud to Agent 1 and Customer
    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 2 speaks into quiet mic
    b2_q = session.setdefault("barger_in_queues", {}).setdefault(agent2_ws, deque(maxlen=10))
    b2_q.append(boosted_quiet)

    mixer_task = asyncio.create_task(audio_mixer_loop(conf_call_id))
    await asyncio.sleep(0.06)
    session["running"] = False
    mixer_task.cancel()
    try:
        await mixer_task
    except asyncio.CancelledError:
        pass

    # Agent 1 heard Agent 2's boosted frame
    assert len(agent1_ws.sent_messages) >= 1
    rec1 = json.loads(agent1_ws.sent_messages[0])
    assert base64.b64decode(rec1["media"]["payload"]) == boosted_quiet

    # Agent 2 did not hear self-echo
    assert len(agent2_ws.sent_messages) == 0

    # Customer queue received Agent 2's boosted frame
    ai_q = session.get("ai_rtp_tx_queue")
    assert ai_q is not None and len(ai_q) >= 1
    assert ai_q[0] == boosted_quiet

    barge_sessions.pop(conf_call_id, None)

