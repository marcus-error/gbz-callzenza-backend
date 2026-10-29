import io
import wave
import struct
import uuid
from pathlib import Path
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.recording_service import (
    save_real_call_recording,
    generate_fallback_wav,
    ensure_call_recording,
    ULAW_TO_PCM,
    RECORDINGS_DIR,
)
from app.db.database import async_session_factory
from app.db.models import Call


def encode_linear_to_mulaw(pcm_val: int) -> int:
    """Helper to encode a 16-bit linear PCM sample to G.711 mu-law byte."""
    BIAS = 0x84
    CLIP = 32635
    sign = (pcm_val >> 8) & 0x80
    if sign != 0:
        pcm_val = -pcm_val
    if pcm_val > CLIP:
        pcm_val = CLIP
    pcm_val += BIAS
    exponent = 7
    mask = 0x4000
    for exp in range(7, -1, -1):
        if pcm_val & mask:
            exponent = exp
            break
        mask >>= 1
    mantissa = (pcm_val >> (exponent + 3)) & 0x0F
    mulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return mulaw_byte


def generate_synthetic_speech_mulaw(num_frames: int = 100, amplitude: int = 3000) -> list[bytes]:
    """Generates synthetic speech frames (20ms, 160 samples per frame at 8000Hz)."""
    frames = []
    for f in range(num_frames):
        frame_bytes = bytearray()
        for i in range(160):
            import math
            sample = int(amplitude * math.sin(2 * math.pi * 300 * (i / 8000.0)))
            frame_bytes.append(encode_linear_to_mulaw(sample))
        frames.append(bytes(frame_bytes))
    return frames


@pytest.mark.asyncio
async def test_save_real_call_recording_success_and_audio_integrity():
    """
    Test 1 & 6: Real conversation frames are decoded to 16-bit linear PCM WAV,
    verifying valid WAV headers, correct sample rate (8000Hz), duration, and speech energy.
    """
    test_call_id = f"call-rec-success-{uuid.uuid4().hex[:8]}"
    frames = generate_synthetic_speech_mulaw(num_frames=150, amplitude=4000)

    rec_url = save_real_call_recording(test_call_id, frames, sample_rate=8000)
    assert rec_url is not None
    assert rec_url == f"/recordings/{test_call_id}_rec.wav"

    wav_path = RECORDINGS_DIR / f"{test_call_id}_rec.wav"
    assert wav_path.exists()
    assert wav_path.stat().st_size > 44

    with wave.open(str(wav_path), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2  # 16-bit linear PCM
        assert wf.getframerate() == 8000
        n_samples = wf.getnframes()
        assert n_samples == 150 * 160  # 24,000 samples = 3.0 seconds
        duration = n_samples / 8000.0
        assert 2.9 <= duration <= 3.1

        raw = wf.readframes(n_samples)
        samples = struct.unpack(f"<{n_samples}h", raw)
        max_amp = max(abs(s) for s in samples)
        assert max_amp > 1000

    try:
        wav_path.unlink()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_connected_call_with_silence_only_suppressed():
    """
    Test 3: Connected call with silence-only (amplitude below speech threshold)
    does not create a fake conversation recording.
    """
    test_call_id = f"call-silence-{uuid.uuid4().hex[:8]}"
    silence_frames = []
    zero_mu = encode_linear_to_mulaw(0)
    for _ in range(60):
        silence_frames.append(bytes([zero_mu] * 160))

    rec_url = save_real_call_recording(test_call_id, silence_frames, sample_rate=8000)
    assert rec_url is None

    wav_path = RECORDINGS_DIR / f"{test_call_id}_rec.wav"
    assert not wav_path.exists()


@pytest.mark.asyncio
async def test_failed_calls_suppress_recording_generation():
    """
    Test 2: Calls that failed before answer (FAILED, BUSY, NO_ANSWER)
    must not generate fake conversation recordings or musical chords.
    """
    uid = uuid.uuid4().hex[:8]
    failed_id = f"call-failed-{uid}"
    busy_id = f"call-busy-{uid}"
    no_ans_id = f"call-noans-{uid}"

    async with async_session_factory() as db:
        failed_call = Call(
            id=failed_id,
            business_id="test-tenant",
            status="FAILED",
            duration_seconds=0,
            client_phone="+15559990001",
        )
        busy_call = Call(
            id=busy_id,
            business_id="test-tenant",
            status="BUSY",
            duration_seconds=0,
            client_phone="+15559990002",
        )
        no_answer_call = Call(
            id=no_ans_id,
            business_id="test-tenant",
            status="NO_ANSWER",
            duration_seconds=0,
            client_phone="+15559990003",
        )
        db.add_all([failed_call, busy_call, no_answer_call])
        await db.commit()

        # ensure_call_recording must NOT generate a recording for failed calls
        res1 = ensure_call_recording(failed_call.id)
        res2 = ensure_call_recording(busy_call.id)
        res3 = ensure_call_recording(no_answer_call.id)

        assert not res1
        assert not res2
        assert not res3

    # Verify GET /recordings/{call_id} returns 404 instead of serving musical audio
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(f"/recordings/{failed_id}_rec.wav")
        assert resp.status_code == 404
        assert "not answered, failed, or contains no speech" in resp.text


@pytest.mark.asyncio
async def test_call_drops_during_conversation_preserves_valid_portion():
    """
    Test 4: When a call connects, conversation occurs, and then drops unexpectedly,
    the captured real conversation frames prior to disconnection are preserved.
    """
    test_call_id = f"call-dropped-{uuid.uuid4().hex[:8]}"
    dialogue_frames = generate_synthetic_speech_mulaw(num_frames=80, amplitude=2500)

    rec_url = save_real_call_recording(test_call_id, dialogue_frames, sample_rate=8000)
    assert rec_url is not None
    assert rec_url == f"/recordings/{test_call_id}_rec.wav"

    wav_path = RECORDINGS_DIR / f"{test_call_id}_rec.wav"
    assert wav_path.exists()
    with wave.open(str(wav_path), "rb") as wf:
        n_samples = wf.getnframes()
        assert n_samples == 80 * 160
        duration = n_samples / 8000.0
        assert 1.55 <= duration <= 1.65

    try:
        wav_path.unlink()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_two_way_audio_mixing_and_no_musical_chord(tmp_path: Path):
    """
    Test 5 & 6: Verify fallback generator does not produce musical chords (sine wave chords)
    and mu-law ITU-T lookup table decodes accurately.
    """
    # 1. Verify ITU-T G.711 expansion table accuracy
    assert len(ULAW_TO_PCM) == 256
    # 0xFF in mu-law is zero level
    assert abs(ULAW_TO_PCM[0xFF]) < 5
    # 0x00 is max positive level (+32124)
    assert ULAW_TO_PCM[0x00] > 30000
    # 0x80 is max negative level (-32124)
    assert ULAW_TO_PCM[0x80] < -30000

    # 2. Check fallback wav generator does not contain harmonic chords
    target_file = tmp_path / "fallback_test.wav"
    wav_bytes = generate_fallback_wav(file_path=target_file, duration_sec=2.0)
    assert target_file.exists()
    with wave.open(str(target_file), "rb") as wf:
        assert wf.getnchannels() == 1
        assert wf.getsampwidth() == 2
        assert wf.getframerate() == 8000
        n_samples = wf.getnframes()
        raw = wf.readframes(n_samples)
        samples = struct.unpack(f"<{n_samples}h", raw)
        # Verify samples are low amplitude dither, NOT loud harmonic sine wave chord
        max_amp = max(abs(s) for s in samples)
        assert max_amp < 150  # Low level comfort noise, not loud 0.45/0.30/0.15 A-minor chord
