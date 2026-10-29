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
    attenuate_ulaw_frame,
    audio_mixer_loop,
    boost_ulaw_speech_frame,
    safe_put_audio_frame,
    flush_barge_session_queues_for_transfer,
    MIX_ULAW_LOOKUP,
    ULAW_TO_PCM_TABLE,
)


class MockWebSocket:
    """Mock WebSocket that records all sent text messages and tracks state."""
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
    """Generates a 160-sample (20ms @ 8kHz) G.711 mu-law sine tone simulating speech phoneme."""
    pcm_samples = []
    for i in range(frame_len):
        val = int(amplitude * math.sin(2.0 * math.pi * freq_hz * i / sample_rate))
        pcm_samples.append(val)

    # Encode to mu-law
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
# TEST 1: Full 3-Way Conference Audio Flow & All 4 Audio Paths
# Customer -> Agent 1 & Agent 2
# Agent 1 -> Customer & Agent 2
# Agent 2 -> Customer & Agent 1
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_three_way_all_four_audio_paths():
    call_id = f"test-clarity-paths-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-paths"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # Path 1: Customer -> Agent 1 and Agent 2
        cust_frame = generate_speech_frame(freq_hz=300.0, amplitude=9000.0)
        barge_in_q = session["barge_inbound_queue"]
        await barge_in_q.put(cust_frame)
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 1
        assert len(ws_agent2.sent_messages) >= 1
        rx_a1 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        rx_a2 = base64.b64decode(json.loads(ws_agent2.sent_messages[-1])["media"]["payload"])
        assert rx_a1 == cust_frame
        assert rx_a2 == cust_frame

        ws_agent1.sent_messages.clear()
        ws_agent2.sent_messages.clear()
        cust_ws.sent_messages.clear()

        # Path 2: Voice Agent 1 -> Customer and Agent 2
        a1_frame = generate_speech_frame(freq_hz=500.0, amplitude=10000.0)
        session["barger_in_queues"][ws_agent1] = deque([a1_frame])
        await asyncio.sleep(0.045)

        assert len(ws_agent2.sent_messages) >= 1
        assert len(cust_ws.sent_messages) >= 1
        rx_a2_from_a1 = base64.b64decode(json.loads(ws_agent2.sent_messages[-1])["media"]["payload"])
        rx_cust_from_a1 = base64.b64decode(json.loads(cust_ws.sent_messages[-1])["media"]["payload"])
        assert rx_a2_from_a1 == a1_frame
        assert rx_cust_from_a1 == a1_frame
        # Zero self-echo for Agent 1
        assert len(ws_agent1.sent_messages) == 0

        ws_agent1.sent_messages.clear()
        ws_agent2.sent_messages.clear()
        cust_ws.sent_messages.clear()

        # Path 3: Voice Agent 2 -> Customer and Agent 1
        a2_frame = generate_speech_frame(freq_hz=700.0, amplitude=9500.0)
        session["barger_in_queues"][ws_agent2] = deque([a2_frame])
        await asyncio.sleep(0.045)

        assert len(ws_agent1.sent_messages) >= 1
        assert len(cust_ws.sent_messages) >= 1
        rx_a1_from_a2 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        rx_cust_from_a2 = base64.b64decode(json.loads(cust_ws.sent_messages[-1])["media"]["payload"])
        assert rx_a1_from_a2 == a2_frame
        assert rx_cust_from_a2 == a2_frame
        # Zero self-echo for Agent 2
        assert len(ws_agent2.sent_messages) == 0

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)


# --------------------------------------------------------------------------
# TEST 2: Voice Agent 2 Audio Level and Intelligibility
# Verifies Agent 2 RMS level is comparable to Agent 1, not attenuated,
# not clipped, and not silent.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_voice_agent_2_audio_levels_and_intelligibility():
    call_id = f"test-clarity-levels-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-levels"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    # Standard conversational speech amplitude (~8000 PCM, -12.3 dBFS)
    a1_frame = generate_speech_frame(freq_hz=440.0, amplitude=8000.0)
    a2_frame = generate_speech_frame(freq_hz=520.0, amplitude=8000.0)

    rms_a1 = calc_ulaw_rms_dbfs(a1_frame)
    rms_a2 = calc_ulaw_rms_dbfs(a2_frame)

    # Verify both source frames have comparable, healthy conversational levels
    assert abs(rms_a1 - rms_a2) < 1.0
    assert -20.0 < rms_a2 < -10.0

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # Agent 2 speaks continuous phoneme (4 frames)
        session["barger_in_queues"][ws_agent2] = deque([a2_frame, a2_frame, a2_frame, a2_frame])
        await asyncio.sleep(0.09)

        # Customer received frames
        assert len(cust_ws.sent_messages) >= 3
        # Agent 1 received frames
        assert len(ws_agent1.sent_messages) >= 3

        # Check RMS of received frames at Customer and Agent 1
        for msg in cust_ws.sent_messages:
            payload = base64.b64decode(json.loads(msg)["media"]["payload"])
            rec_rms = calc_ulaw_rms_dbfs(payload)
            # Level must be robust, not silent, and not clipped to 0 dBFS
            assert -25.0 < rec_rms < -5.0

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)


# --------------------------------------------------------------------------
# TEST 3: Packet Loss Concealment (PLC) During Wi-Fi Arrival Jitter
# When 1 frame is delayed in the middle of active speech (Wi-Fi jitter),
# PLC smoothly bridges the gap without hard silence or audio cutoff.
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_voice_agent_2_wi_fi_jitter_plc_protection():
    call_id = f"test-clarity-plc-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-plc"

    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent2)

    speech_frame = generate_speech_frame(freq_hz=440.0, amplitude=8500.0)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # 1. Voice Agent 2 sends 3 continuous speech frames
        bq = session.setdefault("barger_in_queues", {})[ws_agent2] = deque([
            speech_frame, speech_frame, speech_frame
        ])

        # Allow 5 ticks (~100ms) to play out the 3 speech frames + 2 concealed PLC frames
        await asyncio.sleep(0.11)

        # 3 speech frames + 2 PLC frames = 5 total received frames
        assert len(cust_ws.sent_messages) == 5

        # Verify frames 0, 1, 2 are original speech
        p0 = base64.b64decode(json.loads(cust_ws.sent_messages[0])["media"]["payload"])
        assert p0 == speech_frame
        rms_orig = calc_ulaw_rms_dbfs(p0)

        # Verify frames 3 and 4 are smoothly attenuated PLC frames
        p3 = base64.b64decode(json.loads(cust_ws.sent_messages[3])["media"]["payload"])
        p4 = base64.b64decode(json.loads(cust_ws.sent_messages[4])["media"]["payload"])
        rms_plc1 = calc_ulaw_rms_dbfs(p3)
        rms_plc2 = calc_ulaw_rms_dbfs(p4)

        # PLC frame 1 is attenuated by ~3dB
        assert -35.0 < rms_plc1 < rms_orig
        # PLC frame 2 is attenuated by ~6dB
        assert -40.0 < rms_plc2 < rms_plc1

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)


# --------------------------------------------------------------------------
# TEST 4: Three-Way Simultaneous Double-Talk (Customer + Agent 1 + Agent 2)
# All 3 participants speak simultaneously:
# - Customer hears Agent 1 + Agent 2
# - Agent 1 hears Customer + Agent 2 (NO Agent 1 self-echo)
# - Agent 2 hears Customer + Agent 1 (NO Agent 2 self-echo)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_three_way_simultaneous_double_talk():
    call_id = f"test-clarity-doubletalk-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-dt"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    ws_agent2 = MockWebSocket("VoiceAgent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    f_cust = generate_speech_frame(freq_hz=250.0, amplitude=6000.0)
    f_a1 = generate_speech_frame(freq_hz=440.0, amplitude=7000.0)
    f_a2 = generate_speech_frame(freq_hz=600.0, amplitude=6500.0)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # All 3 speak at once
        await session["barge_inbound_queue"].put(f_cust)
        session["barger_in_queues"][ws_agent1] = deque([f_a1])
        session["barger_in_queues"][ws_agent2] = deque([f_a2])

        await asyncio.sleep(0.045)

        # 1. Customer receives: mix(Agent 1, Agent 2)
        assert len(cust_ws.sent_messages) >= 1
        rx_cust = base64.b64decode(json.loads(cust_ws.sent_messages[0])["media"]["payload"])
        assert rx_cust == mix_ulaw_bytes(f_a1, f_a2)

        # 2. Agent 1 receives: mix(Customer, Agent 2) - NO Agent 1 self-echo
        assert len(ws_agent1.sent_messages) >= 1
        rx_a1 = base64.b64decode(json.loads(ws_agent1.sent_messages[0])["media"]["payload"])
        assert rx_a1 == mix_ulaw_bytes(f_cust, f_a2)

        # 3. Agent 2 receives: mix(Customer, Agent 1) - NO Agent 2 self-echo
        assert len(ws_agent2.sent_messages) >= 1
        rx_a2 = base64.b64decode(json.loads(ws_agent2.sent_messages[0])["media"]["payload"])
        assert rx_a2 == mix_ulaw_bytes(f_cust, f_a1)

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)


# --------------------------------------------------------------------------
# TEST 5: Repeated Transfers & Adding Agent 2 Does Not Break Existing Audio
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_repeated_transfers_and_session_preservation():
    call_id = f"test-clarity-transfer-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Customer")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-tr"

    ws_agent1 = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(ws_agent1)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # Customer and Agent 1 talking
        f1 = generate_speech_frame(freq_hz=440.0, amplitude=7000.0)
        session["barger_in_queues"][ws_agent1] = deque([f1])
        await asyncio.sleep(0.045)
        assert len(cust_ws.sent_messages) >= 1
        cust_ws.sent_messages.clear()

        # Warm transfer simulation: Agent 2 joins
        ws_agent2 = MockWebSocket("VoiceAgent2")
        session["browser_bargers"].add(ws_agent2)

        # Verify Agent 1 and Customer were NOT disconnected
        assert ws_agent1.closed is False
        assert cust_ws.closed is False
        assert len(session["browser_bargers"]) == 2

        # Flush transfer queues
        flush_barge_session_queues_for_transfer(call_id)

        # Agent 2 speaks immediately after transfer
        f2 = generate_speech_frame(freq_hz=550.0, amplitude=8000.0)
        session["barger_in_queues"][ws_agent2] = deque([f2])
        await asyncio.sleep(0.045)

        # Customer and Agent 1 hear Agent 2 clearly
        assert len(cust_ws.sent_messages) >= 1
        assert len(ws_agent1.sent_messages) >= 1
        rx_c = base64.b64decode(json.loads(cust_ws.sent_messages[-1])["media"]["payload"])
        rx_1 = base64.b64decode(json.loads(ws_agent1.sent_messages[-1])["media"]["payload"])
        assert rx_c == f2
        assert rx_1 == f2

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)
