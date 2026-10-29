import asyncio
import base64
import json
import math
import uuid
import pytest
from collections import deque

from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    calc_ulaw_rms_dbfs,
    audio_mixer_loop,
    boost_ulaw_speech_frame,
    flush_barge_session_queues_for_transfer,
    disconnect_browser_bargers,
)


class MockWebSocket:
    """Mock WebSocket that records sent frames and tracks open/close state."""
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


def generate_speech_frame(freq_hz: float = 440.0, sample_rate: int = 8000, frame_len: int = 160, amplitude: float = 8000.0) -> bytes:
    """Generates 160 samples (20ms @ 8kHz) G.711 mu-law PCM sine wave simulating vocal audio."""
    pcm_samples = []
    for i in range(frame_len):
        val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        pcm_samples.append(val)

    ulaw_bytes = bytearray(frame_len)
    for i, s in enumerate(pcm_samples):
        s = max(-32768, min(32767, s))
        sign = 0x80 if s >= 0 else 0x00
        if s < 0:
            s = -s
        s += 0x84
        if s > 32767:
            s = 32767
        exponent = 7
        for exp, threshold in enumerate([0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF]):
            if s <= threshold:
                exponent = exp
                break
        mantissa = (s >> (exponent + 3)) & 0x0F
        ulaw_bytes[i] = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return bytes(ulaw_bytes)


# --------------------------------------------------------------------------
# Requirement 1: Customer joins: microphone and audio communication become active
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_1_customer_joins_audio_active():
    call_id = f"test-conf-scen1-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-1"
    session["running"] = True

    # Customer inbound queue ready
    assert session.get("barge_inbound_queue") is not None
    assert session.get("rtp_leg_queues") is not None

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Feed customer audio frame
        c_frame = generate_speech_frame(freq_hz=250.0, amplitude=8000.0)
        await session["barge_inbound_queue"].put(c_frame)
        await asyncio.sleep(0.045)

        # Conference recorded frames capture customer voice
        assert len(session.get("recorded_frames", [])) >= 1
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 2: Voice Agent 1 joins: Customer & Voice Agent 1 hear each other
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_2_voice_agent_1_joins_bidirectional_audio():
    call_id = f"test-conf-scen2-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-2"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)
    session.setdefault("barger_user_ids", {})[ws_agent1] = "agent-user-1"
    session["supervisor_barging"] = True

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # 1. Customer -> Agent 1
        c_frame = generate_speech_frame(freq_hz=300.0, amplitude=9000.0)
        await session["barge_inbound_queue"].put(c_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 1
        rx_a1_payload = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a1_payload) > -40.0

        # 2. Agent 1 -> Customer
        a1_frame = generate_speech_frame(freq_hz=600.0, amplitude=9000.0)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque()).append(a1_frame)
        await asyncio.sleep(0.045)

        assert len(cust_ws.sent_messages) >= 1
        rx_cust_payload = base64.b64decode(json.loads(cust_ws.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_cust_payload) > -40.0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 3 & 4: Voice Agent 2 & Agent 3 join: all remain audible without interruption
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_3_and_4_multi_agent_join_uninterrupted():
    call_id = f"test-conf-scen34-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-34"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)
    session.setdefault("barger_user_ids", {})[ws_agent1] = "agent-1"

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Agent 1 speaking
        a1_frame = generate_speech_frame(freq_hz=400.0, amplitude=8500.0)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque()).append(a1_frame)
        await asyncio.sleep(0.045)
        assert len(cust_ws.sent_messages) >= 1

        # Agent 2 joins dynamically
        ws_agent2 = MockWebSocket("VoiceAgent2")
        session["browser_bargers"].add(ws_agent2)
        session.setdefault("barger_user_ids", {})[ws_agent2] = "agent-2"

        # Agent 1 continues speaking without stream disruption
        session["barger_in_queues"][ws_agent1].append(a1_frame)
        await asyncio.sleep(0.045)

        # Agent 2 receives Agent 1's speech immediately
        assert len(ws_agent2.sent_messages) >= 1
        rx_a2 = base64.b64decode(json.loads(ws_agent2.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a2) > -40.0

        # Agent 3 joins dynamically
        ws_agent3 = MockWebSocket("VoiceAgent3")
        session["browser_bargers"].add(ws_agent3)
        session.setdefault("barger_user_ids", {})[ws_agent3] = "agent-3"

        # Agent 2 speaks: Customer, Agent 1, and Agent 3 all receive Agent 2's speech
        a2_frame = generate_speech_frame(freq_hz=750.0, amplitude=9000.0)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent2, deque()).append(a2_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 1
        assert len(ws_agent3.sent_messages) >= 1
        rx_a1_from_a2 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        rx_a3_from_a2 = base64.b64decode(json.loads(ws_agent3.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a1_from_a2) > -40.0
        assert calc_ulaw_rms_dbfs(rx_a3_from_a2) > -40.0

        # Now Agent 3 speaks: Agent 1 and Agent 2 receive Agent 3's speech
        a3_frame = generate_speech_frame(freq_hz=850.0, amplitude=9000.0)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent3, deque()).append(a3_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 2
        assert len(ws_agent2.sent_messages) >= 2
        rx_a1_from_a3 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        rx_a2_from_a3 = base64.b64decode(json.loads(ws_agent2.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a1_from_a3) > -40.0
        assert calc_ulaw_rms_dbfs(rx_a2_from_a3) > -40.0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 5: Participants speak simultaneously: clear mixing without suppression
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_5_simultaneous_speech_n_minus_one_mix():
    call_id = f"test-conf-scen5-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-5"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Customer, Agent 1, and Agent 2 speak simultaneously
        c_frame = generate_speech_frame(freq_hz=200.0, amplitude=8000.0)
        a1_frame = generate_speech_frame(freq_hz=500.0, amplitude=8000.0)
        a2_frame = generate_speech_frame(freq_hz=900.0, amplitude=8000.0)

        await session["barge_inbound_queue"].put(c_frame)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque()).append(a1_frame)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent2, deque()).append(a2_frame)

        await asyncio.sleep(0.045)

        # Customer receives mix of Agent 1 + Agent 2
        assert len(cust_ws.sent_messages) >= 1
        rx_c = base64.b64decode(json.loads(cust_ws.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_c) > -35.0

        # Agent 1 receives mix of Customer + Agent 2 (ZERO self-echo)
        assert len(ws_agent1.sent_messages) >= 1
        rx_a1 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a1) > -35.0

        # Agent 2 receives mix of Customer + Agent 1 (ZERO self-echo)
        assert len(ws_agent2.sent_messages) >= 1
        rx_a2 = base64.b64decode(json.loads(ws_agent2.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a2) > -35.0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 6: Participant temporarily stops speaking: mic capture stays active, no overflow
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_6_silence_interval_no_queue_overflow():
    call_id = f"test-conf-scen6-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Agent 1 speaks for 3 frames
        for _ in range(3):
            session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque()).append(
                generate_speech_frame(freq_hz=440.0)
            )
            await asyncio.sleep(0.02)

        # Agent 1 goes silent for 5 ticks (100ms)
        await asyncio.sleep(0.10)

        # Verify barge_outbound_queue did not explode or exceed threshold
        b_out = session.get("barge_outbound_queue")
        assert b_out is None or b_out.qsize() <= 2
        # Agent queue remains clean
        assert len(session["barger_in_queues"][ws_agent1]) == 0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 7: Participant leaves: remaining participants retain audio sessions
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_7_participant_leaves_remaining_retain_audio():
    call_id = f"test-conf-scen7-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-7"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)
    session.setdefault("barger_user_ids", {})[ws_agent1] = "user-agent-1"
    session.setdefault("barger_user_ids", {})[ws_agent2] = "user-agent-2"

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Disconnect Agent 2 specifically
        await disconnect_browser_bargers(call_id, target_user_id="user-agent-2")

        # Verify Agent 2 is closed and removed
        assert ws_agent2.closed is True
        assert ws_agent2 not in session["browser_bargers"]

        # Verify Agent 1 is STILL CONNECTED and active
        assert ws_agent1.closed is False
        assert ws_agent1 in session["browser_bargers"]

        # Test audio between Customer and Agent 1 remains fully operational
        c_frame = generate_speech_frame(freq_hz=350.0)
        await session["barge_inbound_queue"].put(c_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 1
        rx_a1 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a1) > -40.0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 8: Recovery from temporary failure without corrupting other sessions
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_8_reconnect_recovery_uninterrupted_peers():
    call_id = f"test-conf-scen8-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)
    session.setdefault("barger_user_ids", {})[ws_agent1] = "user-1"

    # Agent 2 experiences a socket drop
    ws_agent2_dropped = MockWebSocket("VoiceAgent2_old")
    session["browser_bargers"].add(ws_agent2_dropped)
    session.setdefault("barger_user_ids", {})[ws_agent2_dropped] = "user-2"

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))
    try:
        # Old socket fails
        await disconnect_browser_bargers(call_id, target_ws=ws_agent2_dropped)
        assert ws_agent2_dropped not in session["browser_bargers"]

        # Reconnect Agent 2 with new socket
        ws_agent2_new = MockWebSocket("VoiceAgent2_reconnected")
        session["browser_bargers"].add(ws_agent2_new)
        session.setdefault("barger_user_ids", {})[ws_agent2_new] = "user-2"

        # Agent 1 speech reaches reconnected Agent 2
        a1_frame = generate_speech_frame(freq_hz=480.0)
        session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque()).append(a1_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent2_new.sent_messages) >= 1
        rx_a2 = base64.b64decode(json.loads(ws_agent2_new.sent_messages[-1])["media"]["payload"])
        assert calc_ulaw_rms_dbfs(rx_a2) > -40.0
    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass


# --------------------------------------------------------------------------
# Requirement 9: Conference ends: resources cleaned up correctly
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_9_conference_end_cleanup():
    call_id = f"test-conf-scen9-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    # Disconnect all bargers when conference ends
    await disconnect_browser_bargers(call_id)

    assert ws_agent1.closed is True
    assert ws_agent2.closed is True
    assert len(session["browser_bargers"]) == 0
    assert session.get("supervisor_barging") is False


# --------------------------------------------------------------------------
# Requirement 10: Repeated transfers do not create duplicates or reset active media
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_10_repeated_transfers_active_media_preserved():
    call_id = f"test-conf-scen10-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)
    session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, deque())
    session.setdefault("barger_jitter_states", {})[ws_agent1] = {"total_plc_frames": 0}

    # Simulate repeated transfer flush calls during warm transfers
    flush_barge_session_queues_for_transfer(call_id)
    flush_barge_session_queues_for_transfer(call_id)
    flush_barge_session_queues_for_transfer(call_id)

    # Active bargers must NOT have had their in-queues or jitter states wiped
    assert ws_agent1 in session["browser_bargers"]
    assert ws_agent1 in session["barger_in_queues"]
    assert ws_agent1 in session.get("barger_jitter_states", {})
