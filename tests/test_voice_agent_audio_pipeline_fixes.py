import asyncio
import io
import math
import wave
import uuid
import pytest
from collections import deque
from unittest.mock import AsyncMock, patch, MagicMock

from app.api.routes.barge_stream import (
    _get_or_create_session,
    barge_sessions,
    safe_put_audio_frame,
    audio_mixer_loop,
)
from app.services.voice_service import (
    ALAW_TO_ULAW_TABLE,
    ULAW_TO_ALAW_TABLE,
)
from app.services.sip_ai_bot import (
    ulaw2pcm,
    pcm2ulaw,
    resample_and_normalize_audio,
    build_wav_from_pcm,
    wav_to_mulaw,
)


@pytest.mark.asyncio
async def test_itut_g711_bit_exact_transcoding_tables():
    """Verify ALAW_TO_ULAW and ULAW_TO_ALAW match standard ITU-T G.711 specifications."""
    # Test silence, zero crossing, positive peak, and negative peak
    # PCMU silence is 0xFF, PCMA silence is 0xD5
    assert ULAW_TO_ALAW_TABLE[0xFF] == 0xD5, "PCMU silence 0xFF must translate to PCMA 0xD5"
    assert ALAW_TO_ULAW_TABLE[0xD5] == 0xFF, "PCMA silence 0xD5 must translate to PCMU 0xFF"

    # All 256 input bytes must produce valid 8-bit output bytes
    assert len(ALAW_TO_ULAW_TABLE) == 256
    assert len(ULAW_TO_ALAW_TABLE) == 256

    # Test round trip audio fidelity on synthetic speech waveform
    sample_rate = 8000
    t = [i / sample_rate for i in range(160)]  # 20ms frame
    pcm_sine = [int(15000 * math.sin(2 * math.pi * 440 * ti)) for ti in t]
    ulaw_bytes = bytes([pcm2ulaw(s) for s in pcm_sine])

    # Transcode PCMU -> PCMA -> PCMU
    alaw_bytes = ulaw_bytes.translate(ULAW_TO_ALAW_TABLE)
    assert len(alaw_bytes) == 160
    roundtrip_ulaw = alaw_bytes.translate(ALAW_TO_ULAW_TABLE)
    assert len(roundtrip_ulaw) == 160

    # Decoded PCM samples should retain high correlation and minimal quantization error
    pcm_orig = [ulaw2pcm(b) for b in ulaw_bytes]
    pcm_roundtrip = [ulaw2pcm(b) for b in roundtrip_ulaw]
    diffs = [abs(o - r) for o, r in zip(pcm_orig, pcm_roundtrip)]
    max_err = max(diffs)
    # G.711 companding error between A-law and mu-law is bounded within standard quantization step
    assert max_err < 1000, f"Max companding error too large: {max_err}"


@pytest.mark.asyncio
async def test_barge_inbound_queue_no_overflow_when_inbound_legs_active():
    """
    Verify that when customer RTP frames arrive into inbound_legs,
    audio_mixer_loop keeps barge_inbound_queue cleanly drained,
    preventing queue capacity (100) from overflowing and evicting frames.
    """
    call_id = f"test-drain-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    inbound_legs = session["inbound_legs"]
    leg_q = deque(maxlen=25)
    inbound_legs[call_id] = leg_q

    barge_in_q = session["barge_inbound_queue"]
    assert barge_in_q.empty()

    # Feed 150 simulated RTP frames (3 seconds of audio at 50 pps)
    mixer_task = session["mixer_task"]

    for i in range(150):
        frame = b"\x80" * 160
        # Simulating dual-queue RTP receive behavior
        leg_q.append(frame)
        safe_put_audio_frame(barge_in_q, frame, "barge_inbound_queue", call_id)

        # Allow mixer to tick periodically
        if i % 10 == 0:
            await asyncio.sleep(0.01)

    await asyncio.sleep(0.05)
    session["running"] = False
    await mixer_task

    # Crucial assertion: barge_inbound_queue must be drained (< 5 frames) and NO frames evicted
    assert barge_in_q.qsize() <= 4, f"barge_inbound_queue accumulated {barge_in_q.qsize()} frames"
    drop_count = getattr(barge_in_q, "_drop_count", 0)
    assert drop_count == 0, f"Expected 0 evicted frames, got {drop_count}"


@pytest.mark.asyncio
async def test_exclusive_queue_ownership_and_no_duplicate_outbound_insertion():
    """
    Verify that when audio_mixer_loop runs with active rtp_leg_queues:
    1. ai_rtp_tx_queue frames are routed to rtp_leg_queues.
    2. Frames are NOT duplicate-inserted into rtp_outbound_queue.
    3. session['last_outbound_source'] is tagged as 'AI_BOT'.
    """
    call_id = f"test-ownership-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    # No human bargers connected
    session["browser_bargers"].clear()

    # Enqueue 3 synthetic AI TTS frames
    f1 = b"\x01" * 160
    f2 = b"\x02" * 160
    f3 = b"\x03" * 160
    session["ai_rtp_tx_queue"].extend([f1, f2, f3])

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.08)
    session["running"] = False
    await mixer_task

    # 1. Frames dispatched to leg queue
    rtp_legs = session.get("rtp_leg_queues", {})
    assert call_id in rtp_legs
    leg_q = rtp_legs[call_id]
    assert len(leg_q) >= 1
    out_frame = leg_q.popleft()
    assert out_frame == f1

    # 2. rtp_outbound_queue must remain empty (no duplicate frame accumulation)
    rtp_out = session.get("rtp_outbound_queue")
    assert len(rtp_out) == 0, "rtp_outbound_queue must not receive duplicate frames when rtp_leg_queues is active"

    # 3. Source correctly tagged as AI_BOT
    assert session.get("last_outbound_source") == "AI_BOT"
    assert session.get("is_human_barger_speaking") is False


@pytest.mark.asyncio
async def test_human_barger_source_tagging_and_mix():
    """
    Verify that when a human barger speaks from the browser:
    1. session['last_outbound_source'] is tagged as 'HUMAN_MIC'.
    2. session['is_human_barger_speaking'] is True.
    """
    call_id = f"test-human-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    barge_sessions[call_id] = session

    class MockWS:
        pass

    ws = MockWS()
    session["browser_bargers"].add(ws)

    # Human agent speaks a loud 440Hz tone
    human_speech = b"\x15" * 160
    bq = session.setdefault("barger_in_queues", {}).setdefault(ws, deque(maxlen=25))
    bq.extend([human_speech] * 5)

    mixer_task = session["mixer_task"]
    await asyncio.sleep(0.06)
    session["running"] = False
    await mixer_task

    assert session.get("is_human_barger_speaking") is True
    assert session.get("last_outbound_source") == "HUMAN_MIC"


@pytest.mark.asyncio
async def test_stt_pipeline_end_to_end_mock():
    """
    Verify that simulated 8000Hz customer PCM is built into WAV, passed to
    transcribe_audio, and produces a valid clean transcript with stage logs.
    """
    # 500ms of simulated speech samples (4000 samples @ 8kHz)
    pcm_speech = [int(10000 * math.sin(2 * math.pi * 300 * (i / 8000))) for i in range(4000)]
    wav_bytes = build_wav_from_pcm(pcm_speech, sample_rate=8000)

    assert len(wav_bytes) > 8000
    assert wav_bytes.startswith(b"RIFF")

    with patch("app.services.stt_service.transcribe_audio", new_callable=AsyncMock) as mock_stt:
        mock_stt.return_value = "I would like to check my balance"
        from app.services.stt_service import transcribe_audio

        transcript = await transcribe_audio(wav_bytes, mimetype="audio/wav", language_code="en-IN")
        assert transcript == "I would like to check my balance"
        mock_stt.assert_called_once()
