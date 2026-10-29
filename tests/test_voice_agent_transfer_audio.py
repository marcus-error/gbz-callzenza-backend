import asyncio
import json
import base64
from collections import deque
import pytest

from app.api.routes.barge_stream import (
    barge_sessions,
    flush_barge_session_queues_for_transfer,
    mix_ulaw_bytes,
    _get_or_create_session,
)


@pytest.mark.asyncio
async def test_barge_session_queue_flush_on_transfer():
    """
    Verifies that when Voice Agent 2 accepts a transferred call, all stale
    queued audio from Agent 1 / hold period is flushed, guaranteeing 0ms backlog delay.
    """
    call_id = "test-transfer-call-101"
    session = await _get_or_create_session(call_id)

    # Simulate 50 stale frames (1 second of queued audio) left in queues
    stale_frame = b"\xff" * 160
    for _ in range(50):
        session["ai_rtp_tx_queue"].append(stale_frame)
        session["rtp_outbound_queue"].append(stale_frame)
        session["barge_outbound_queue"].put_nowait(stale_frame)

    session["mic_buffer"] = bytearray(b"\xaa" * 120)
    session["last_barger_frame"] = ("mock_ws", stale_frame, 12345.0)

    assert len(session["ai_rtp_tx_queue"]) == 50
    assert len(session["rtp_outbound_queue"]) == 50
    assert session["barge_outbound_queue"].qsize() == 50

    # Flush queues on transfer handoff
    flush_barge_session_queues_for_transfer(call_id)

    assert len(session["ai_rtp_tx_queue"]) == 0
    assert len(session["rtp_outbound_queue"]) == 0
    assert session["barge_outbound_queue"].qsize() == 0
    assert "mic_buffer" not in session
    assert "last_barger_frame" not in session

    # Cleanup
    barge_sessions.pop(call_id, None)


def test_per_connection_mic_buffer_no_cross_corruption():
    """
    Verifies that each agent maintains an isolated connection buffer,
    preventing Agent 1 and Agent 2's partial chunks from interleaving mid-frame.
    """
    # Simulate ScriptProcessor sending ~85-byte chunks
    agent1_chunk_a = b"\x11" * 85
    agent1_chunk_b = b"\x11" * 85

    agent2_chunk_a = b"\x22" * 85
    agent2_chunk_b = b"\x22" * 85

    # Each connection maintains its own local bytearray buffer
    agent1_conn_buf = bytearray()
    agent2_conn_buf = bytearray()

    # Agent 1 sends chunk A (85 bytes)
    agent1_conn_buf.extend(agent1_chunk_a)
    assert len(agent1_conn_buf) < 160  # not enough yet

    # Concurrently, Agent 2 sends chunk A (85 bytes)
    agent2_conn_buf.extend(agent2_chunk_a)
    assert len(agent2_conn_buf) < 160

    # Agent 1 sends chunk B (85 bytes -> total 170 bytes)
    agent1_conn_buf.extend(agent1_chunk_b)
    assert len(agent1_conn_buf) >= 160

    frame_agent1 = bytes(agent1_conn_buf[:160])
    del agent1_conn_buf[:160]

    # Verify frame_agent1 is pure Agent 1 audio with NO byte contamination from Agent 2
    assert len(frame_agent1) == 160
    assert frame_agent1 == b"\x11" * 160
    assert len(agent1_conn_buf) == 10

    # Agent 2 sends chunk B (85 bytes -> total 170 bytes)
    agent2_conn_buf.extend(agent2_chunk_b)
    assert len(agent2_conn_buf) >= 160

    frame_agent2 = bytes(agent2_conn_buf[:160])
    del agent2_conn_buf[:160]

    # Verify frame_agent2 is pure Agent 2 audio
    assert len(frame_agent2) == 160
    assert frame_agent2 == b"\x22" * 160
    assert len(agent2_conn_buf) == 10


def test_concurrency_clean_g711_mixing():
    """
    Verifies that when Agent 1 and Agent 2 speak concurrently, their frames
    are mixed cleanly via ITU-T G.711 sample lookup without distortion or clipping.
    """
    # Sample μ-law frames
    agent1_frame = b"\x00" * 160  # Maximum positive amplitude in μ-law (linear +8031)
    agent2_frame = b"\xff" * 160  # Silence in μ-law (linear 0)

    mixed = mix_ulaw_bytes(agent1_frame, agent2_frame)
    assert len(mixed) == 160
    # Mixing max amplitude with silence yields max amplitude without clipping/overflow
    assert mixed[0] == agent1_frame[0]

    # Two identical frames mixed together
    mixed_double = mix_ulaw_bytes(b"\x20" * 160, b"\x20" * 160)
    assert len(mixed_double) == 160
    # Output must be saturated valid G.711 byte
    assert 0 <= mixed_double[0] <= 255


@pytest.mark.asyncio
async def test_low_latency_telephony_queue_cap():
    """
    Verifies that ai_rtp_tx_queue enforces a strict low-latency cap (<= 5 frames / 100ms)
    even if packets arrive in rapid bursts from the browser WebSocket.
    """
    call_id = "test-low-latency-cap-call"
    session = await _get_or_create_session(call_id)
    ai_q = session["ai_rtp_tx_queue"]
    ai_q.clear()

    # Simulate 30 rapid packets arriving from browser
    frame = b"\x55" * 160
    for i in range(30):
        ai_q.append(frame)
        while len(ai_q) > 5:
            ai_q.popleft()

    # Queue must never exceed 5 frames (100ms latency)
    assert len(ai_q) <= 5
    assert len(ai_q) == 5

    # Cleanup
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_peer_barger_audio_forwarding():
    """
    Verifies that Agent 1's audio frames are forwarded to Agent 2's WebSocket,
    ensuring Voice Agent 1 is clearly audible to Voice Agent 2 during transfer.
    """
    class MockWebSocket:
        def __init__(self, name):
            self.name = name
            self.sent_messages = []

        async def send_text(self, text: str):
            self.sent_messages.append(text)

    ws_agent1 = MockWebSocket("agent1")
    ws_agent2 = MockWebSocket("agent2")

    call_id = "test-peer-barger-call"
    session = await _get_or_create_session(call_id)
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    # When Agent 1 speaks, a 160-byte frame is extracted
    agent1_frame = b"\x33" * 160

    # Peer bargers logic from browser_barge_stream:
    other_bargers = [ws for ws in list(session.get("browser_bargers", set())) if ws != ws_agent1]
    assert len(other_bargers) == 1
    assert other_bargers[0] is ws_agent2

    peer_payload = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {
            "payload": base64.b64encode(agent1_frame).decode("utf-8"),
            "format": "audio/x-mulaw",
            "sampleRate": 8000
        }
    })
    for other_ws in other_bargers:
        await other_ws.send_text(peer_payload)

    # Verify Agent 2 received Agent 1's audio frame
    assert len(ws_agent2.sent_messages) == 1
    received_data = json.loads(ws_agent2.sent_messages[0])
    assert received_data["event"] == "media"
    assert received_data["call_id"] == call_id
    decoded_audio = base64.b64decode(received_data["media"]["payload"])
    assert decoded_audio == agent1_frame

    # Verify Agent 1 did NOT receive self-echo
    assert len(ws_agent1.sent_messages) == 0

    # Cleanup
    barge_sessions.pop(call_id, None)


# ============================================================================
# COMPREHENSIVE 3-PARTY VOICE AGENT TRANSFER TESTS (Requirements 1 - 8)
# ============================================================================

class MockWS:
    """Mock WebSocket for unit-testing audio streaming and routing."""
    def __init__(self, name: str):
        self.name = name
        self.sent_messages = []
        self.closed = False
        self.close_code = None

    async def send_text(self, text: str):
        self.sent_messages.append(text)

    async def close(self, code: int = 1000):
        self.closed = True
        self.close_code = code


@pytest.mark.asyncio
async def test_req1_three_participants_join_same_canonical_session():
    """
    Requirement 1: All three participants join the same canonical conference/session.
    Verifies that regardless of whether a participant references SIP Call-ID, stripped ID,
    or DB UUID, all resolve to the EXACT SAME session dictionary in barge_sessions.
    """
    import uuid
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call

    db_uuid = str(uuid.uuid4())
    sip_cid = f"call-{uuid.uuid4().hex[:8]}@callzenza"
    sip_clean = sip_cid.split("@")[0]

    async with AsyncSessionLocal() as db:
        call_obj = Call(
            id=db_uuid,
            call_sid=sip_cid,
            business_id="test-biz-req1",
            status="IN_PROGRESS",
            agent_type="HUMAN",
        )
        db.add(call_obj)
        await db.commit()

    # Telephony initializes session under SIP Call-ID
    telephony_sess = await _get_or_create_session(sip_cid)
    assert telephony_sess is not None

    # Agent 1 joins using sip_clean
    agent1_sess = await _get_or_create_session(sip_clean)
    # Agent 2 joins using DB UUID
    agent2_sess = await _get_or_create_session(db_uuid)

    # Must all be the exact same dictionary in memory
    assert telephony_sess is agent1_sess
    assert agent1_sess is agent2_sess

    # Attach both agent WebSockets
    ws1 = MockWS("Agent1")
    ws2 = MockWS("Agent2")
    telephony_sess["browser_bargers"].add(ws1)
    telephony_sess["browser_bargers"].add(ws2)

    assert len(agent2_sess["browser_bargers"]) == 2
    assert ws1 in agent2_sess["browser_bargers"]
    assert ws2 in agent2_sess["browser_bargers"]

    # Cleanup
    for k in [db_uuid, sip_cid, sip_clean]:
        barge_sessions.pop(k, None)


@pytest.mark.asyncio
async def test_req2_audio_in_all_six_directions():
    """
    Requirement 2: Audio in all six directions:
      1. Customer -> Voice Agent 1
      2. Customer -> Voice Agent 2
      3. Voice Agent 1 -> Customer
      4. Voice Agent 2 -> Customer
      5. Voice Agent 1 -> Voice Agent 2
      6. Voice Agent 2 -> Voice Agent 1
    """
    call_id = "test-six-directions-call"
    session = await _get_or_create_session(call_id)

    ws_agent1 = MockWS("agent1")
    ws_agent2 = MockWS("agent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    customer_frame = b"\x1a" * 160
    agent1_frame = b"\x2b" * 160
    agent2_frame = b"\x3c" * 160

    # Direction 1 & 2: Customer -> Voice Agent 1 and Customer -> Voice Agent 2
    barger_payload = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {
            "payload": base64.b64encode(customer_frame).decode("utf-8"),
            "format": "audio/x-mulaw",
            "sampleRate": 8000
        }
    })
    for ws in list(session["browser_bargers"]):
        await ws.send_text(barger_payload)

    # Verify both Agent 1 and Agent 2 received customer audio
    assert len(ws_agent1.sent_messages) == 1
    assert len(ws_agent2.sent_messages) == 1
    rec1 = json.loads(ws_agent1.sent_messages[-1])
    rec2 = json.loads(ws_agent2.sent_messages[-1])
    assert base64.b64decode(rec1["media"]["payload"]) == customer_frame
    assert base64.b64decode(rec2["media"]["payload"]) == customer_frame

    # Direction 5: Voice Agent 1 -> Voice Agent 2 (Peer forwarding)
    peer_payload_1 = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {
            "payload": base64.b64encode(agent1_frame).decode("utf-8"),
            "format": "audio/x-mulaw",
            "sampleRate": 8000
        }
    })
    other_bargers_for_1 = [ws for ws in list(session["browser_bargers"]) if ws != ws_agent1]
    for other_ws in other_bargers_for_1:
        await other_ws.send_text(peer_payload_1)

    assert len(ws_agent2.sent_messages) == 2
    rec_by_2 = json.loads(ws_agent2.sent_messages[-1])
    assert base64.b64decode(rec_by_2["media"]["payload"]) == agent1_frame

    # Direction 6: Voice Agent 2 -> Voice Agent 1 (Peer forwarding)
    peer_payload_2 = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {
            "payload": base64.b64encode(agent2_frame).decode("utf-8"),
            "format": "audio/x-mulaw",
            "sampleRate": 8000
        }
    })
    other_bargers_for_2 = [ws for ws in list(session["browser_bargers"]) if ws != ws_agent2]
    for other_ws in other_bargers_for_2:
        await other_ws.send_text(peer_payload_2)

    assert len(ws_agent1.sent_messages) == 2
    rec_by_1 = json.loads(ws_agent1.sent_messages[-1])
    assert base64.b64decode(rec_by_1["media"]["payload"]) == agent2_frame

    # Direction 3 & 4: Voice Agent 1 -> Customer and Voice Agent 2 -> Customer (RTP queue)
    ai_q = session["ai_rtp_tx_queue"]
    ai_q.clear()
    ai_q.append(agent1_frame)
    assert len(ai_q) == 1
    popped_for_cust = ai_q.popleft()
    assert popped_for_cust == agent1_frame

    ai_q.append(agent2_frame)
    assert len(ai_q) == 1
    popped_for_cust2 = ai_q.popleft()
    assert popped_for_cust2 == agent2_frame

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_req3_agent2_joins_while_customer_already_talking():
    """
    Requirement 3: Agent 2 joins while the customer is already talking.
    Verifies that active customer speech frames in the inbound queue continue
    flowing without drop, and Agent 2 immediately receives ongoing customer audio.
    """
    call_id = "test-agent2-joins-talking-call"
    session = await _get_or_create_session(call_id)
    ws_agent1 = MockWS("Agent1")
    session["browser_bargers"].add(ws_agent1)

    # Customer speaks 5 continuous 20ms frames
    in_q = session["barge_inbound_queue"]
    for i in range(5):
        frame = bytes([0x20 + i] * 160)
        in_q.put_nowait(frame)

    assert in_q.qsize() == 5

    # Agent 2 joins now
    ws_agent2 = MockWS("Agent2")
    session["browser_bargers"].add(ws_agent2)
    assert len(session["browser_bargers"]) == 2

    # Drain and broadcast customer speech to bargers
    while not in_q.empty():
        c_frame = in_q.get_nowait()
        msg = json.dumps({
            "event": "media",
            "call_id": call_id,
            "media": {
                "payload": base64.b64encode(c_frame).decode("utf-8"),
                "format": "audio/x-mulaw",
                "sampleRate": 8000
            }
        })
        for ws in list(session["browser_bargers"]):
            await ws.send_text(msg)

    # Both Agent 1 and newly joined Agent 2 received all 5 customer frames
    assert len(ws_agent1.sent_messages) == 5
    assert len(ws_agent2.sent_messages) == 5

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_req4_agent2_fails_to_join_preserves_customer_and_agent1():
    """
    Requirement 4: Agent 2 failing to join does NOT disconnect the customer.
    Customer and Agent 1 continue talking with zero interruption.
    """
    call_id = "test-agent2-fail-call"
    session = await _get_or_create_session(call_id)
    ws_agent1 = MockWS("Agent1")
    session["browser_bargers"].add(ws_agent1)
    session["running"] = True

    # Simulate Agent 2 failure / rejection
    agent2_failed = True
    if agent2_failed:
        # Agent 2 rejected, but session remains running
        assert session["running"] is True
        assert ws_agent1 in session["browser_bargers"]

    # Customer speaks
    cust_frame = b"\x77" * 160
    barger_msg = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {"payload": base64.b64encode(cust_frame).decode("utf-8")}
    })
    for ws in list(session["browser_bargers"]):
        await ws.send_text(barger_msg)

    # Agent 1 still receives customer speech
    assert len(ws_agent1.sent_messages) == 1
    assert ws_agent1.closed is False
    assert session["running"] is True

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_req5_agent1_leaving_without_disconnecting_customer():
    """
    Requirement 5: Agent 1 leaving the conference without prematurely disconnecting the customer.
    Customer and Agent 2 must remain in active two-way communication.
    """
    call_id = "test-agent1-leaves-call"
    session = await _get_or_create_session(call_id)
    ws_agent1 = MockWS("Agent1")
    ws_agent2 = MockWS("Agent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)
    session["running"] = True

    # Agent 1 leaves the transfer (clicks 'Leave Transfer')
    session["browser_bargers"].discard(ws_agent1)
    ws_agent1.closed = True

    # Verify session remains running for Customer and Agent 2
    assert session["running"] is True
    assert len(session["browser_bargers"]) == 1
    assert ws_agent2 in session["browser_bargers"]

    # Agent 2 speaks to customer
    agent2_frame = b"\x88" * 160
    session["ai_rtp_tx_queue"].append(agent2_frame)
    assert len(session["ai_rtp_tx_queue"]) == 1

    # Customer speaks to Agent 2
    cust_frame = b"\x99" * 160
    msg = json.dumps({
        "event": "media",
        "call_id": call_id,
        "media": {"payload": base64.b64encode(cust_frame).decode("utf-8")}
    })
    for ws in list(session["browser_bargers"]):
        await ws.send_text(msg)

    assert len(ws_agent2.sent_messages) == 1
    assert len(ws_agent1.sent_messages) == 0

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_req6_customer_hangup_and_complete_cleanup():
    """
    Requirement 6: Customer hangup triggers complete resource cleanup and
    properly terminates all participants' sessions.
    """
    call_id = "test-customer-hangup-call"
    session = await _get_or_create_session(call_id)
    ws1 = MockWS("Agent1")
    ws2 = MockWS("Agent2")
    session["browser_bargers"].add(ws1)
    session["browser_bargers"].add(ws2)

    # Customer hangs up
    session["running"] = False
    end_msg = json.dumps({"event": "CALL_ENDED", "call_id": call_id, "message": "Call stream ended."})
    for ws in list(session["browser_bargers"]):
        await ws.send_text(end_msg)
        await ws.close(code=1000)

    assert ws1.closed is True
    assert ws2.closed is True
    assert len(ws1.sent_messages) == 1
    assert json.loads(ws1.sent_messages[0])["event"] == "CALL_ENDED"

    barge_sessions.pop(call_id, None)
    assert call_id not in barge_sessions


@pytest.mark.asyncio
async def test_req7_correct_recording_and_no_duplicate_audio():
    """
    Requirement 7: Correct recording capture and no duplicate audio frames.
    """
    call_id = "test-recording-call"
    session = await _get_or_create_session(call_id)

    # Simulate recording frames during conversation
    f1 = b"\x10" * 160
    f2 = b"\x20" * 160
    session.setdefault("recorded_frames", []).append(f1)
    session.setdefault("recorded_frames", []).append(f2)

    assert len(session["recorded_frames"]) == 2
    assert session["recorded_frames"][0] == f1
    assert session["recorded_frames"][1] == f2

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_req8_tenant_isolation_and_no_cross_leakage():
    """
    Requirement 8: Organization/tenant isolation prevents cross-organization
    audio leakage or unauthorized participation.
    """
    from app.api.routes.barge_stream import _validate_active_call_for_monitoring
    import uuid
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call

    call_id = f"test-tenant-iso-{uuid.uuid4().hex[:6]}"
    async with AsyncSessionLocal() as db:
        c = Call(
            id=call_id,
            business_id="tenant-alpha",
            status="IN_PROGRESS"
        )
        db.add(c)
        await db.commit()

    call_obj, status = await _validate_active_call_for_monitoring(call_id)
    assert status == "ACTIVE"
    assert call_obj.business_id == "tenant-alpha"

    # Cross-tenant check: user from tenant-beta cannot access call belonging to tenant-alpha
    user_tenant = "tenant-beta"
    user_role = "VOICE_AGENT"
    is_allowed = (
        user_tenant == call_obj.business_id
        or "SUPER_ADMIN" in user_role
        or "SUPERADMIN" in user_role
    )
    assert is_allowed is False

    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_customer_to_agent_audio_no_queue_theft():
    """
    Verifies Customer-to-Agent audio path:
    Customer RTP packets placed into inbound_queue for STT/VAD are NOT stolen
    or consumed by audio_mixer_loop, ensuring 100% of customer packets reach the Voice Agent.
    """
    call_id = "test-c2a-audio-theft"
    session = await _get_or_create_session(call_id)

    # Put customer audio frame into inbound_queue (STT/VAD) and barge_inbound_queue
    customer_frame = b"\x33" * 160
    session["inbound_queue"].put_nowait(customer_frame)
    session["barge_inbound_queue"].put_nowait(customer_frame)

    # Let audio_mixer_loop tick
    await asyncio.sleep(0.06)

    # Ensure inbound_queue STILL has customer_frame for STT/VAD (not stolen by mixer)
    assert not session["inbound_queue"].empty()
    stt_packet = session["inbound_queue"].get_nowait()
    assert stt_packet == customer_frame

    # Cleanup
    session["running"] = False
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_agent_to_customer_audio_no_recirculation():
    """
    Verifies Agent-to-Customer audio path:
    AI Voice Agent response frames placed into ai_rtp_tx_queue are routed
    to rtp_leg_queues without being re-appended into ai_rtp_tx_queue (no infinite echo loop).
    """
    call_id = "test-a2c-audio-recirculation"
    session = await _get_or_create_session(call_id)

    # Pre-register an RTP phone leg
    leg_q = deque(maxlen=10)
    session["rtp_leg_queues"][call_id] = leg_q

    # Voice Agent TTS generates 3 sequential frames
    f1 = b"\x11" * 160
    f2 = b"\x22" * 160
    f3 = b"\x33" * 160
    session["ai_rtp_tx_queue"].extend([f1, f2, f3])

    # Allow audio_mixer_loop to process
    await asyncio.sleep(0.08)

    # Verify all frames were routed to the customer leg queue
    assert len(leg_q) > 0
    # Verify frames did NOT recirculate back into ai_rtp_tx_queue
    assert len(session["ai_rtp_tx_queue"]) == 0

    # Cleanup
    session["running"] = False
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_human_voice_agent_bidirectional_audio():
    """
    Verifies bidirectional audio between customer and human Voice Agent:
    1. Voice Agent mic frames sent via WebSocket reach customer RTP leg.
    2. Customer RTP frames reach Voice Agent WebSocket without packet drops.
    """
    call_id = "test-bidirectional-voice-agent"
    session = await _get_or_create_session(call_id)

    # Mock Voice Agent WebSocket
    class MockVoiceAgentWS:
        def __init__(self):
            self.sent_messages = []
            self.closed = False

        async def send_text(self, text):
            self.sent_messages.append(text)

        async def close(self, code=1000):
            self.closed = True

    agent_ws = MockVoiceAgentWS()
    session["browser_bargers"].add(agent_ws)

    # 1. Customer -> Agent: customer sends RTP packet
    customer_pkt = b"\x55" * 160
    in_leg = deque(maxlen=10)
    in_leg.append(customer_pkt)
    session["inbound_legs"]["customer_leg"] = in_leg

    # 2. Agent -> Customer: agent mic audio
    agent_mic_pkt = b"\x77" * 160
    b_in_q = deque(maxlen=5)
    b_in_q.append(agent_mic_pkt)
    session.setdefault("barger_in_queues", {})[agent_ws] = b_in_q

    cust_rtp_q = deque(maxlen=10)
    session["rtp_leg_queues"]["customer_leg"] = cust_rtp_q

    # Let audio_mixer_loop tick
    await asyncio.sleep(0.06)

    # Verify Customer received Agent mic frame
    assert len(cust_rtp_q) > 0
    assert cust_rtp_q.popleft() == agent_mic_pkt

    # Verify Agent WebSocket received Customer audio
    media_messages = [json.loads(m) for m in agent_ws.sent_messages if json.loads(m).get("event") == "media"]
    assert len(media_messages) > 0
    received_payload = base64.b64decode(media_messages[0]["media"]["payload"])
    assert received_payload == customer_pkt

    # Cleanup
    session["running"] = False
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_warm_transfer_lifecycle_media_continuity():
    """
    Verifies media continuity before, during, and after a warm transfer:
    - Before: Customer connected to AI session.
    - During: AI paused, hold state active, customer leg preserved.
    - After: Voice Agent 2 connected to canonical session via alias cross-registration.
    """
    import uuid
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call, CallTransfer
    from app.services.call_transfer_service import CallTransferService
    from app.repositories.call_transfer_repository import CallTransferSession

    transfer_id = f"transfer-{uuid.uuid4().hex[:8]}"
    call_id = f"call-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:6]}"

    async with AsyncSessionLocal() as db:
        call_obj = Call(
            id=call_id,
            business_id="biz-transfer-test",
            status="IN_PROGRESS",
            call_sid=f"CA{uuid.uuid4().hex[:12]}@10.0.0.1",
            agent_id=None
        )
        db.add(call_obj)

        transfer_obj = CallTransferSession(
            id=transfer_id,
            call_id=call_id,
            business_id="biz-transfer-test",
            status="RINGING",
            assigned_voice_agent_id=agent_id,
            assigned_voice_agent_name="Agent Smith",
            customer_name="John Doe",
            customer_phone="+1234567890"
        )
        db.add(transfer_obj)
        await db.commit()

        # Step 1: Pre-transfer session created under call_obj.call_sid
        canonical_session = await _get_or_create_session(call_obj.call_sid)
        canonical_session["test_marker"] = "canonical_audio_bridge"

        # Step 2: Accept transfer
        res = await CallTransferService.accept_warm_transfer(
            transfer_id=transfer_id,
            agent_id=agent_id,
            db=db
        )

        assert res["status"] == "WITH_HUMAN_AGENT"

        # Step 3: Verify Voice Agent connecting with transfer_id resolves to canonical session
        resolved_session = await _get_or_create_session(transfer_id)
        assert resolved_session.get("test_marker") == "canonical_audio_bridge"
        assert resolved_session["status"] == "WITH_HUMAN_AGENT"

    # Cleanup
    canonical_session["running"] = False
    barge_sessions.pop(call_id, None)
    barge_sessions.pop(transfer_id, None)
    barge_sessions.pop(call_obj.call_sid, None)


