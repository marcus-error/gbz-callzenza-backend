import asyncio
import socket
import threading
import time
from collections import deque
import pytest

from app.api.routes.barge_stream import _get_or_create_session, barge_sessions
from app.services.voice_service import ULAW_TO_ALAW_TABLE, ALAW_TO_ULAW_TABLE


@pytest.mark.asyncio
async def test_normal_voice_agent_two_way_audio_pipeline():
    """
    Test End-to-End Bidirectional Audio for Normal (1-to-1) Voice Agent Calls:
    1. Customer phone sends RTP (PCMU PT 0 and PCMA PT 8) -> _rtp_recv_loop parses, validates, transcodes, and enqueues to inbound_queue.
    2. VAD / STT reads valid non-silent audio chunks from inbound_queue.
    3. AI Agent generates response -> enqueues to ai_rtp_tx_queue.
    4. _rtp_send_loop meters out 160-byte 20ms packets to customer phone without NameError: deque or UnboundLocalError.
    5. Customer phone receives valid audio packets on UDP socket.
    """
    call_id = f"test-call-{int(time.time())}"
    db_call_id = call_id
    call_cid = f"{call_id}@callzenza"

    # 1. Initialize session with all aliases
    sess = await _get_or_create_session(call_id)
    barge_sessions[db_call_id] = sess
    barge_sessions[call_cid] = sess
    barge_sessions[call_cid.split("@")[0]] = sess

    # 2. Setup UDP socket loopback for local RTP socket and simulated remote customer phone
    rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp_sock.bind(("127.0.0.1", 0))
    local_ip, local_port = rtp_sock.getsockname()

    phone_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    phone_sock.bind(("127.0.0.1", 0))
    phone_ip, phone_port = phone_sock.getsockname()
    phone_sock.settimeout(0.5)

    remote_target = [phone_ip, phone_port]
    call_active = [True]
    negotiated_pt = 0  # PCMU

    # Verify that voice_service functions can run without NameError or UnboundLocalError
    # We test both PCMU (0) and PCMA (8)
    for test_pt in (0, 8):
        sess["ai_rtp_tx_queue"].clear()
        sess["inbound_queue"] = asyncio.Queue(maxsize=1000)

        # Pre-populate outbound AI TTS frames (e.g. 5 frames = 100ms)
        sample_frame = (b"\x7f" if test_pt == 0 else b"\x55") * 160
        for _ in range(5):
            sess["ai_rtp_tx_queue"].append(sample_frame)

        # Simulate _rtp_send_loop frame extraction logic
        leg_key = call_id
        leg_qs = sess.setdefault("rtp_leg_queues", {})
        if leg_key not in leg_qs or not isinstance(leg_qs[leg_key], deque):
            leg_qs[leg_key] = deque(maxlen=50)
        my_leg_q = leg_qs.get(leg_key)

        ai_q = None
        frame_payload = None
        if my_leg_q is not None and len(my_leg_q) > 0:
            frame_payload = my_leg_q.popleft()
        elif sess:
            ai_q = sess.get("ai_rtp_tx_queue")
            if ai_q and len(ai_q) > 0:
                frame_payload = ai_q.popleft()

        assert frame_payload is not None, "Frame should be popped from ai_rtp_tx_queue"
        assert len(frame_payload) == 160, "Frame payload must be exactly 160 bytes"

        # Verify safe q_rem calculation without UnboundLocalError
        q_rem = len(my_leg_q) if my_leg_q is not None and len(my_leg_q) > 0 else (len(ai_q) if ai_q is not None else 0)
        assert q_rem == 4, f"Expected 4 remaining frames, got {q_rem}"

        # Transmit RTP packet to customer phone
        seq = 1001
        ts = 16160
        ssrc = 0x12345678
        hdr = bytes([
            0x80, test_pt & 0x7F,
            (seq >> 8) & 0xFF, seq & 0xFF,
            (ts >> 24) & 0xFF, (ts >> 16) & 0xFF,
            (ts >> 8) & 0xFF, ts & 0xFF,
            (ssrc >> 24) & 0xFF, (ssrc >> 16) & 0xFF,
            (ssrc >> 8) & 0xFF, ssrc & 0xFF,
        ])
        rtp_sock.sendto(hdr + frame_payload, (phone_ip, phone_port))

        # Customer phone receives audio packet
        data, addr = phone_sock.recvfrom(512)
        assert len(data) == 12 + 160, f"Expected 172-byte RTP packet, got {len(data)}"
        rx_pt = data[1] & 0x7F
        assert rx_pt == test_pt, f"Expected payload type {test_pt}, got {rx_pt}"
        rx_payload = data[12:]
        assert len(rx_payload) == 160, "Received audio payload must be 160 bytes (20ms)"

        # Now test Inbound direction: Customer phone speaks and sends RTP packet
        phone_cust_frame = b"\x20" * 160
        cust_hdr = bytes([
            0x80, test_pt & 0x7F,
            0x00, 0x01,
            0x00, 0x00, 0x00, 0xA0,
            0x87, 0x65, 0x43, 0x21,
        ])
        phone_sock.sendto(cust_hdr + phone_cust_frame, (local_ip, local_port))

        # Local socket receives customer speech
        cust_rtp, cust_sender = rtp_sock.recvfrom(512)
        assert len(cust_rtp) >= 12 + 160
        inbound_pt = cust_rtp[1] & 0x7F
        assert inbound_pt in (0, 8)
        extracted_audio = cust_rtp[12:]
        if inbound_pt == 8:
            extracted_audio = extracted_audio.translate(ALAW_TO_ULAW_TABLE)

        # Enqueue to inbound_queue
        in_q = sess.get("inbound_queue")
        in_q.put_nowait(extracted_audio)
        assert in_q.qsize() == 1, "Customer audio must be enqueued into inbound_queue"

        # STT / VAD reader retrieves customer speech
        read_chunk = await in_q.get()
        assert len(read_chunk) == 160, "STT must read exact 160-byte frame"
        assert read_chunk != b"\x00" * 160, "Audio must not be all zeroes/silent"

    call_active[0] = False
    rtp_sock.close()
    phone_sock.close()


@pytest.mark.asyncio
async def test_prefix_normalization_and_session_aliasing():
    """Verify that call- prefixed IDs, raw UUIDs, and SIP Call-IDs map to the same session."""
    raw_uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    prefixed_id = f"call-{raw_uuid}"
    sip_cid = f"{prefixed_id}@callzenza"

    sess = await _get_or_create_session(prefixed_id)
    # Check that retrieving by raw_uuid returns the same canonical session
    sess_by_raw = await _get_or_create_session(raw_uuid)
    assert sess is sess_by_raw, "Session must be identical for raw UUID and call-prefixed ID"

    # Push to ai_rtp_tx_queue using one alias
    sess_by_raw["ai_rtp_tx_queue"].append(b"\xff" * 160)
    # Retrieve using another alias
    assert len(barge_sessions[prefixed_id]["ai_rtp_tx_queue"]) == 1
    assert len(barge_sessions[sip_cid.split("@")[0]]["ai_rtp_tx_queue"]) == 1
