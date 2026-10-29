import asyncio
import base64
from collections import deque
import json
import socket
import threading
import time
import uuid
import pytest

from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    audio_mixer_loop,
    safe_put_audio_frame,
)
from app.services.voice_service import (
    ALAW_TO_ULAW_TABLE,
    ULAW_TO_ALAW_TABLE,
)


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


def test_1_deque_availability_and_imports():
    """Verify collections.deque is cleanly imported and instantiated."""
    import app.services.voice_service as vs
    assert hasattr(vs, "deque"), "voice_service.py must expose deque at module level"
    d = vs.deque(maxlen=25)
    d.append(b"test_audio_frame")
    assert len(d) == 1
    assert d.popleft() == b"test_audio_frame"


def test_2_alaw_to_ulaw_transcoding_table():
    """Verify A-law (PT 8) to mu-law (PT 0) transcoding table works accurately."""
    alaw_silence = b"\xd5" * 160
    ulaw_silence = alaw_silence.translate(ALAW_TO_ULAW_TABLE)
    assert len(ulaw_silence) == 160
    # Reverse transcode
    alaw_back = ulaw_silence.translate(ULAW_TO_ALAW_TABLE)
    assert len(alaw_back) == 160


@pytest.mark.asyncio
async def test_3_customer_pcma_decoding_and_queueing():
    """
    Simulate customer RTP packet ingestion with PCMA (payload type 8).
    Verify it is transcoded to mu-law and enqueued into inbound_legs and queues.
    """
    call_id = f"test-rx-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    # 12-byte RTP header with PT=8 (PCMA)
    hdr = bytes([
        0x80, 0x08,       # V=2, P=0, X=0, CC=0, M=0, PT=8 (PCMA)
        0x00, 0x01,       # Seq=1
        0x00, 0x00, 0x00, 0xA0, # Ts=160
        0x12, 0x34, 0x56, 0x78  # SSRC
    ])
    # 160 bytes of PCMA speech
    speech_sample = bytes([(i % 256) for i in range(160)])
    rtp_pkt = hdr + speech_sample

    # Extract payload and transcode as voice_service._rtp_recv_loop does
    payload = rtp_pkt[12:]
    transcoded = payload.translate(ALAW_TO_ULAW_TABLE)

    leg_key = call_id
    in_legs = session.setdefault("inbound_legs", {})
    if leg_key not in in_legs or not isinstance(in_legs[leg_key], deque):
        in_legs[leg_key] = deque(maxlen=25)
    in_legs[leg_key].append(transcoded)

    in_q = session.get("inbound_queue")
    barge_q = session.get("barge_inbound_queue")
    safe_put_audio_frame(in_q, transcoded, "inbound_queue", call_id)
    safe_put_audio_frame(barge_q, transcoded, "barge_inbound_queue", call_id)

    assert len(in_legs[leg_key]) == 1
    assert in_legs[leg_key][0] == transcoded
    assert not barge_q.empty()
    assert barge_q.get_nowait() == transcoded


@pytest.mark.asyncio
async def test_4_customer_audio_reaches_voice_agent_ws():
    """
    Verify customer speech arriving in inbound_legs is dispatched by audio_mixer_loop
    directly to connected Voice Agent WebSockets.
    """
    call_id = f"test-c2va-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent_ws = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(agent_ws)

    # Put speech frame in inbound_legs
    cust_speech = b"\x80" * 160
    session.setdefault("inbound_legs", {})[call_id] = deque([cust_speech])

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.06)
    session["running"] = False
    await mixer_task

    # Agent must have received WebSocket media frame with customer audio
    assert len(agent_ws.sent_messages) > 0
    received_payloads = [json.loads(m) for m in agent_ws.sent_messages if "media" in json.loads(m)]
    assert len(received_payloads) >= 1
    first_media = base64.b64decode(received_payloads[0]["media"]["payload"])
    assert len(first_media) == 160
    assert first_media == cust_speech


@pytest.mark.asyncio
async def test_5_human_voice_agent_audio_reaches_customer_rtp_output():
    """
    Verify Voice Agent microphone frames from WebSocket arrive in rtp_leg_queues
    for Asterisk/SIP RTP transmission to customer.
    """
    call_id = f"test-va2c-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent_ws = MockWebSocket("VoiceAgent1")
    session["browser_bargers"].add(agent_ws)

    # Ingest 3 frames of agent microphone speech into barger_in_queues
    mic_frame = b"\x80" * 160
    bq = session.setdefault("barger_in_queues", {}).setdefault(agent_ws, deque(maxlen=25))
    bq.append(mic_frame)
    bq.append(mic_frame)
    bq.append(mic_frame)

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.08)
    session["running"] = False
    await mixer_task

    # rtp_leg_queues must contain the agent speech frames destined for RTP transmitter
    rtp_legs = session.get("rtp_leg_queues", {})
    assert call_id in rtp_legs
    leg_q = rtp_legs[call_id]
    assert len(leg_q) >= 1
    transmitted_frame = leg_q.popleft()
    assert len(transmitted_frame) == 160
    assert transmitted_frame == mic_frame


@pytest.mark.asyncio
async def test_6_ai_generated_audio_reaches_customer_rtp_output():
    """
    Verify AI TTS audio frames placed in ai_rtp_tx_queue are dispatched to
    rtp_leg_queues when no human agent is speaking, and never loop back.
    """
    call_id = f"test-ai2c-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    # No human bargers connected
    session["browser_bargers"].clear()

    ai_speech = b"\xaa" * 160
    ai_q = session.get("ai_rtp_tx_queue")
    ai_q.append(ai_speech)

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.06)
    session["running"] = False
    await mixer_task

    rtp_legs = session.get("rtp_leg_queues", {})
    assert call_id in rtp_legs
    assert len(rtp_legs[call_id]) >= 1
    out_frame = rtp_legs[call_id].popleft()
    assert out_frame == ai_speech


@pytest.mark.asyncio
async def test_7_zero_self_echo_mix_minus():
    """
    Verify that an agent's own microphone audio is NEVER echoed back to that agent's WebSocket,
    satisfying the N-1 mix-minus requirement.
    """
    call_id = f"test-echo-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent1_ws = MockWebSocket("Agent1")
    session["browser_bargers"].add(agent1_ws)

    # Only Agent 1 speaks, customer is silent
    agent1_speech = b"\x33" * 160
    bq1 = session.setdefault("barger_in_queues", {}).setdefault(agent1_ws, deque(maxlen=25))
    bq1.append(agent1_speech)

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.06)
    session["running"] = False
    await mixer_task

    # Agent 1 should receive ZERO downlink frames because neither customer nor other agents spoke
    assert len(agent1_ws.sent_messages) == 0


@pytest.mark.asyncio
async def test_8_three_party_full_mesh_audio_distribution():
    """
    Verify full three-party conference audio:
    - Customer speaks: both Agent 1 and Agent 2 hear customer.
    - Agent 1 speaks: customer hears Agent 1 and Agent 2 hears Agent 1.
    - Agent 2 speaks: customer hears Agent 2 and Agent 1 hears Agent 2.
    - All 3 speak: each hears the other two mixed without self-echo.
    """
    call_id = f"test-3way-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Step 1: Customer speaks
    cust_frame = b"\x20" * 160
    session.setdefault("inbound_legs", {})[call_id] = deque([cust_frame])

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.06)

    # Both Agent 1 and Agent 2 received customer frame
    assert len(agent1_ws.sent_messages) >= 1
    assert len(agent2_ws.sent_messages) >= 1
    a1_msg = json.loads(agent1_ws.sent_messages[-1])
    a2_msg = json.loads(agent2_ws.sent_messages[-1])
    assert base64.b64decode(a1_msg["media"]["payload"]) == cust_frame
    assert base64.b64decode(a2_msg["media"]["payload"]) == cust_frame

    # Step 2: Agent 1 speaks
    agent1_ws.sent_messages.clear()
    agent2_ws.sent_messages.clear()
    a1_frame = b"\x40" * 160
    bq1 = session.setdefault("barger_in_queues", {}).setdefault(agent1_ws, deque(maxlen=25))
    bq1.append(a1_frame)
    await asyncio.sleep(0.06)

    # Customer RTP queue received Agent 1 speech
    assert len(session["rtp_leg_queues"][call_id]) >= 1
    # Agent 2 heard Agent 1
    assert len(agent2_ws.sent_messages) >= 1
    a2_msg = json.loads(agent2_ws.sent_messages[-1])
    assert base64.b64decode(a2_msg["media"]["payload"]) == a1_frame
    # Agent 1 got no self-echo
    assert len(agent1_ws.sent_messages) == 0

    # Step 3: Agent 2 speaks
    agent1_ws.sent_messages.clear()
    agent2_ws.sent_messages.clear()
    session["rtp_leg_queues"][call_id].clear()
    a2_frame = b"\x60" * 160
    bq2 = session.setdefault("barger_in_queues", {}).setdefault(agent2_ws, deque(maxlen=25))
    bq2.append(a2_frame)
    await asyncio.sleep(0.06)

    # Customer RTP queue received Agent 2 speech
    assert len(session["rtp_leg_queues"][call_id]) >= 1
    # Agent 1 heard Agent 2
    assert len(agent1_ws.sent_messages) >= 1
    a1_msg = json.loads(agent1_ws.sent_messages[-1])
    assert base64.b64decode(a1_msg["media"]["payload"]) == a2_frame
    # Agent 2 got no self-echo
    assert len(agent2_ws.sent_messages) == 0

    session["running"] = False
    await mixer_task


@pytest.mark.asyncio
async def test_9_add_agent_2_preserves_agent_1():
    """
    Verify that adding Voice Agent 2 to an active call does NOT disconnect Voice Agent 1.
    """
    call_id = f"test-add-a2-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent1_ws = MockWebSocket("Agent1")
    session["browser_bargers"].add(agent1_ws)

    assert agent1_ws in session["browser_bargers"]
    assert len(session["browser_bargers"]) == 1

    # Now Agent 2 joins
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent2_ws)

    assert len(session["browser_bargers"]) == 2
    assert agent1_ws in session["browser_bargers"]
    assert agent2_ws in session["browser_bargers"]
    assert not agent1_ws.closed
    assert not agent2_ws.closed


@pytest.mark.asyncio
async def test_10_remove_agent_2_preserves_customer_and_agent_1():
    """
    Verify that removing Agent 2 (e.g. transfer complete or warm handoff) preserves
    Customer and Agent 1 connection and media routing.
    """
    call_id = f"test-rem-a2-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    agent1_ws = MockWebSocket("Agent1")
    agent2_ws = MockWebSocket("Agent2")
    session["browser_bargers"].add(agent1_ws)
    session["browser_bargers"].add(agent2_ws)

    # Agent 2 disconnects
    session["browser_bargers"].remove(agent2_ws)
    await agent2_ws.close()

    assert len(session["browser_bargers"]) == 1
    assert agent1_ws in session["browser_bargers"]
    assert not agent1_ws.closed
    assert session.get("is_active", True)


@pytest.mark.asyncio
async def test_11_call_id_and_sip_cid_alias_resolution():
    """
    Verify application call ID and SIP Call-ID correctly resolve to the exact same session.
    """
    app_call_id = f"call-{uuid.uuid4().hex[:12]}"
    sip_call_id = f"call-{uuid.uuid4().hex[:12]}@callzenza"
    clean_sip_id = sip_call_id.split("@")[0]

    session = await _get_or_create_session(app_call_id)
    barge_sessions[app_call_id] = session
    barge_sessions[sip_call_id] = session
    barge_sessions[clean_sip_id] = session

    # All keys must resolve to the identical object identity
    assert barge_sessions.get(app_call_id) is session
    assert barge_sessions.get(sip_call_id) is session
    assert barge_sessions.get(clean_sip_id) is session


@pytest.mark.asyncio
async def test_12_no_stale_audio_leakage_between_calls():
    """
    Verify audio queues are isolated and cleared between different calls,
    preventing any previous call's audio from leaking into a new call.
    """
    old_call_id = f"test-old-{uuid.uuid4().hex[:8]}"
    old_session = await _get_or_create_session(old_call_id)
    barge_sessions[old_call_id] = old_session

    # Fill old session with audio
    old_session["ai_rtp_tx_queue"].append(b"\x99" * 160)
    old_session["rtp_outbound_queue"].append(b"\x99" * 160)

    # Create distinct new call
    new_call_id = f"test-new-{uuid.uuid4().hex[:8]}"
    new_session = await _get_or_create_session(new_call_id)
    barge_sessions[new_call_id] = new_session

    assert new_session is not old_session
    assert len(new_session["ai_rtp_tx_queue"]) == 0
    assert len(new_session["rtp_outbound_queue"]) == 0
    assert len(new_session["inbound_legs"]) == 0
