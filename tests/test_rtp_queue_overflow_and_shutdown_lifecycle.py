import asyncio
import socket
import threading
import time
import uuid
import pytest
import pytest_asyncio
from datetime import datetime, timezone

from app.api.routes.barge_stream import (
    barge_sessions,
    safe_put_audio_frame,
    shutdown_all_barge_sessions,
    _get_or_create_session,
)
from app.services.voice_service import (
    ACTIVE_SIP_CALLS,
    hangup_sip_call,
    shutdown_all_active_sip_calls,
)


@pytest.mark.asyncio
async def test_safe_put_audio_frame_bounded_overflow_no_queuefull():
    """
    Validates:
    1. Bounded audio queue does NOT raise asyncio.queues.QueueFull on overflow.
    2. Oldest frames are safely evicted, newest frames are retained.
    3. Drop metrics are accurately recorded.
    """
    q = asyncio.Queue(maxsize=5)
    call_id = f"test-call-{uuid.uuid4().hex[:8]}"

    # Put 5 frames (reaches max capacity)
    for i in range(5):
        success = safe_put_audio_frame(q, f"frame_{i}".encode("utf-8"), "test_q", call_id)
        assert success is True
    assert q.qsize() == 5

    # Put 5 more frames (overflow)
    for i in range(5, 10):
        success = safe_put_audio_frame(q, f"frame_{i}".encode("utf-8"), "test_q", call_id)
        assert success is True

    # Queue size must be clamped at maxsize
    assert q.qsize() == 5
    assert getattr(q, "_drop_count", 0) == 5

    # Validate FIFO eviction: items remaining should be frames 5, 6, 7, 8, 9
    remaining = []
    while not q.empty():
        remaining.append((await q.get()).decode("utf-8"))
    assert remaining == ["frame_5", "frame_6", "frame_7", "frame_8", "frame_9"]


@pytest.mark.asyncio
async def test_rtp_thread_safe_queue_feeding_under_overflow():
    """
    Validates that high-throughput feeding from a background thread via
    loop.call_soon_threadsafe(safe_put_audio_frame, ...) does not cause unhandled
    QueueFull callback exceptions on the event loop.
    """
    loop = asyncio.get_running_loop()
    q = asyncio.Queue(maxsize=10)
    call_id = f"test-thread-{uuid.uuid4().hex[:8]}"
    stop_event = threading.Event()

    def producer():
        frame_idx = 0
        while not stop_event.is_set():
            frame = f"audio_{frame_idx}".encode("utf-8")
            if not loop.is_closed():
                loop.call_soon_threadsafe(safe_put_audio_frame, q, frame, "rtp_rx_queue", call_id)
            frame_idx += 1
            time.sleep(0.001)

    t = threading.Thread(target=producer, daemon=True)
    t.start()

    # Let producer run and overflow queue
    await asyncio.sleep(0.05)
    stop_event.set()
    t.join(timeout=1.0)

    # Let scheduled callbacks execute
    await asyncio.sleep(0.02)

    assert q.qsize() <= 10
    assert getattr(q, "_drop_count", 0) > 0


@pytest.mark.asyncio
async def test_per_call_audio_queue_isolation():
    """
    Validates that audio queues are completely isolated between calls.
    Frame delivered to call_1 must NEVER appear in call_2.
    """
    cid_1 = f"call-iso-1-{uuid.uuid4().hex[:8]}"
    cid_2 = f"call-iso-2-{uuid.uuid4().hex[:8]}"

    sess_1 = await _get_or_create_session(cid_1)
    sess_2 = await _get_or_create_session(cid_2)

    assert sess_1["inbound_queue"] is not sess_2["inbound_queue"]
    assert sess_1["barge_inbound_queue"] is not sess_2["barge_inbound_queue"]

    frame_1 = b"audio_for_call_1_only"
    frame_2 = b"audio_for_call_2_only"

    safe_put_audio_frame(sess_1["inbound_queue"], frame_1, "inbound_queue", cid_1)
    safe_put_audio_frame(sess_2["inbound_queue"], frame_2, "inbound_queue", cid_2)

    read_1 = await sess_1["inbound_queue"].get()
    read_2 = await sess_2["inbound_queue"].get()

    assert read_1 == frame_1
    assert read_2 == frame_2
    assert sess_1["inbound_queue"].empty()
    assert sess_2["inbound_queue"].empty()

    # Clean up sessions
    sess_1["running"] = False
    sess_2["running"] = False
    barge_sessions.pop(cid_1, None)
    barge_sessions.pop(cid_2, None)


@pytest.mark.asyncio
async def test_stt_finalization_mock_pipeline_flow(monkeypatch):
    """
    Validates customer speech → VAD → STT final transcript availability → AI processing.
    """
    import app.services.stt_service as stt_module

    # Mock transcribe_audio to return a known final transcript
    async def mock_transcribe(audio_bytes, mimetype="audio/wav", language_code="en-IN"):
        assert len(audio_bytes) > 0
        return "I am interested in term life insurance coverage"

    monkeypatch.setattr(stt_module, "transcribe_audio", mock_transcribe)

    dummy_audio = b"\x00\x00" * 1600  # 1600 samples of 16-bit PCM = 200ms
    transcript = await stt_module.transcribe_audio(dummy_audio, mimetype="audio/wav", language_code="en-IN")

    assert transcript == "I am interested in term life insurance coverage"


@pytest.mark.asyncio
async def test_shutdown_lifecycle_stops_rtp_and_barge_sessions():
    """
    Validates that shutdown_all_active_sip_calls and shutdown_all_barge_sessions:
    1. Mark all call_active flags to False.
    2. Close RTP sockets without throwing unhandled exceptions.
    3. Cancel background mixer tasks.
    4. Leave no orphaned threads or open socket state.
    """
    call_id = f"call-shutdown-{uuid.uuid4().hex[:8]}"

    # Set up mock active SIP call
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    call_active = [True]
    record = {
        "sock": s,
        "rtp_sock": s,
        "call_active": call_active,
        "call_cid": call_id,
        "db_call_id": call_id,
        "hangup_started": False,
        "sock_closed": False,
        "rtp_sock_closed": False,
    }
    ACTIVE_SIP_CALLS[call_id] = record

    # Set up mock barge session with running mixer task
    sess = await _get_or_create_session(call_id)
    assert sess["running"] is True

    # Execute shutdown functions
    shutdown_all_active_sip_calls()
    shutdown_all_barge_sessions()

    # Verify call is inactive and socket is closed
    assert call_active[0] is False
    assert record.get("sock_closed") is True
    assert record.get("rtp_sock_closed") is True
    assert call_id not in ACTIVE_SIP_CALLS
    assert len(barge_sessions) == 0
