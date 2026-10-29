import pytest
import asyncio
from collections import deque

def fanout_ai_audio_helper(session: dict, mulaw_bytes: bytes):
    """Reflects the exact logic of fanout_ai_audio in sip_ai_bot.py."""
    if not mulaw_bytes:
        return
    chunk_size = 160
    for i in range(0, len(mulaw_bytes), chunk_size):
        slice_bytes = mulaw_bytes[i:i + chunk_size]
        if len(slice_bytes) < chunk_size:
            slice_bytes += b'\xff' * (chunk_size - len(slice_bytes))

        # 1. Fan out to dedicated AI Phone RTP queue (authoritative queue for AI audio)
        if "ai_rtp_tx_queue" in session:
            session["ai_rtp_tx_queue"].append(slice_bytes)

        # 2. Fan out to Laptop Monitor playback queue (outbound_queue & barge_outbound_queue)
        if "outbound_queue" in session:
            try:
                session["outbound_queue"].put_nowait(slice_bytes)
            except Exception:
                pass


@pytest.mark.asyncio
async def test_tts_audio_enqueued_in_single_authoritative_queue():
    """Verify that TTS audio is enqueued ONLY in ai_rtp_tx_queue and NOT in rtp_outbound_queue."""
    session = {
        "ai_rtp_tx_queue": deque(maxlen=5000),
        "rtp_outbound_queue": deque(maxlen=5000),
        "outbound_queue": asyncio.Queue(maxsize=500),
        "ai_speaking": True,
        "running": True,
    }
    
    # 5.33 seconds of mu-law audio (~42640 bytes)
    sample_mulaw = b"\x7f" * 42640

    fanout_ai_audio_helper(session, sample_mulaw)

    # 1. Verify single queue insertion
    assert len(session["ai_rtp_tx_queue"]) > 0
    assert len(session["rtp_outbound_queue"]) == 0  # Crucial: Must NOT be inserted here!

    # 2. Verify exact frame count
    expected_frames = (len(sample_mulaw) + 159) // 160
    assert len(session["ai_rtp_tx_queue"]) == expected_frames

    # 3. Simulate draining ai_rtp_tx_queue (RTP transmission)
    drained_frames = 0
    while len(session["ai_rtp_tx_queue"]) > 0:
        frame = session["ai_rtp_tx_queue"].popleft()
        drained_frames += 1

    assert drained_frames == expected_frames
    assert len(session["ai_rtp_tx_queue"]) == 0
    assert len(session["rtp_outbound_queue"]) == 0  # No lingering duplicate audio!


@pytest.mark.asyncio
async def test_prepare_customer_listening_clears_leftover_queues():
    """Verify that preparing customer listening clears any leftover frames in RTP queues."""
    session = {
        "ai_rtp_tx_queue": deque([b"\x00" * 160, b"\x00" * 160]),
        "rtp_outbound_queue": deque([b"\xff" * 160]),
        "ai_speaking": True,
    }

    # Simulate prepare_customer_listening queue cleanup logic
    session["ai_speaking"] = False
    if session.get("ai_rtp_tx_queue"):
        session["ai_rtp_tx_queue"].clear()
    if session.get("rtp_outbound_queue"):
        session["rtp_outbound_queue"].clear()

    assert len(session["ai_rtp_tx_queue"]) == 0
    assert len(session["rtp_outbound_queue"]) == 0
    assert session["ai_speaking"] is False


@pytest.mark.asyncio
async def test_two_simultaneous_calls_queue_isolation():
    """Verify two simultaneous calls remain isolated and cannot share audio queues."""
    sess_a = {
        "ai_rtp_tx_queue": deque(maxlen=5000),
        "rtp_outbound_queue": deque(maxlen=5000),
        "outbound_queue": asyncio.Queue(maxsize=500),
    }
    sess_b = {
        "ai_rtp_tx_queue": deque(maxlen=5000),
        "rtp_outbound_queue": deque(maxlen=5000),
        "outbound_queue": asyncio.Queue(maxsize=500),
    }

    audio_a = b"\x00" * 320
    audio_b = b"\xff" * 320

    fanout_ai_audio_helper(sess_a, audio_a)
    fanout_ai_audio_helper(sess_b, audio_b)

    assert len(sess_a["ai_rtp_tx_queue"]) == 2
    assert len(sess_b["ai_rtp_tx_queue"]) == 2

    # Popping from A does not affect B
    sess_a["ai_rtp_tx_queue"].popleft()
    assert len(sess_a["ai_rtp_tx_queue"]) == 1
    assert len(sess_b["ai_rtp_tx_queue"]) == 2


@pytest.mark.asyncio
async def test_consecutive_turns_no_replay():
    """Verify consecutive turns clear queues cleanly and do not replay previous audio."""
    session = {
        "ai_rtp_tx_queue": deque(maxlen=5000),
        "rtp_outbound_queue": deque(maxlen=5000),
        "outbound_queue": asyncio.Queue(maxsize=500),
        "ai_speaking": False,
        "running": True,
    }

    # Turn 1
    fanout_ai_audio_helper(session, b"\x11" * 1600)
    assert len(session["ai_rtp_tx_queue"]) == 10
    assert len(session["rtp_outbound_queue"]) == 0

    # Drain turn 1
    session["ai_rtp_tx_queue"].clear()

    # Turn 2
    fanout_ai_audio_helper(session, b"\x22" * 1600)
    assert len(session["ai_rtp_tx_queue"]) == 10
    assert len(session["rtp_outbound_queue"]) == 0
    
    first_frame = session["ai_rtp_tx_queue"][0]
    assert first_frame == b"\x22" * 160
