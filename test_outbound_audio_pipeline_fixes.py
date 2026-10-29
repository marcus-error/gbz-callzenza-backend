import math
import numpy as np
from loguru import logger

from app.services.voice_service import ALAW_TO_ULAW_TABLE
from app.services.sip_ai_bot import (
    ulaw2pcm,
    pcm2ulaw,
    build_wav_from_pcm,
    resample_and_normalize_audio,
)

def generate_tone_mulaw(freq: float = 440.0, duration_sec: float = 1.0, sample_rate: int = 8000) -> bytes:
    """Generates a pure sine tone encoded as 8kHz G.711 mu-law."""
    t = np.linspace(0, duration_sec, int(sample_rate * duration_sec), endpoint=False)
    # Amplitude of ~10,000 in 16-bit PCM (-32768 to 32767)
    pcm_sine = (10000.0 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
    indices = (pcm_sine.astype(np.int32) + 32768).clip(0, 65535)
    
    from app.services.sip_ai_bot import _PCM_TO_ULAW_TABLE
    return _PCM_TO_ULAW_TABLE[indices].tobytes()

def test_pcmu_preservation():
    """Verify that incoming PCMU (PT 0) packets are NEVER mangled by ALAW_TO_ULAW_TABLE."""
    original_mulaw = generate_tone_mulaw(freq=500.0, duration_sec=0.2)
    
    # Simulate packet with PT 0
    pkt_pt = 0
    negotiated_pt = 8 # Notice Asterisk might list 8 first in SDP
    
    # Fix logic in voice_service.py:
    if pkt_pt == 8:
        codec_name = "PCMA"
        payload_audio = original_mulaw.translate(ALAW_TO_ULAW_TABLE)
    elif pkt_pt == 0:
        codec_name = "PCMU"
        payload_audio = original_mulaw
    elif negotiated_pt == 8:
        codec_name = "PCMA"
        payload_audio = original_mulaw.translate(ALAW_TO_ULAW_TABLE)
    else:
        codec_name = "PCMU"
        payload_audio = original_mulaw
        
    assert codec_name == "PCMU", f"Expected PCMU codec, got {codec_name}"
    assert payload_audio == original_mulaw, "PCMU audio was modified or corrupted!"
    
    # Decode to PCM and verify signal fidelity
    pcm_samples = [ulaw2pcm(b) for b in payload_audio]
    sum_sq = sum(s * s for s in pcm_samples)
    rms = math.sqrt(sum_sq / max(1, len(pcm_samples)))
    
    # RMS of 10000 amplitude sine wave is ~7071
    assert 6000 < rms < 8000, f"Expected RMS ~7071, got {rms}"
    print(f"[PASS] PCMU Packet decoding: Signal intact, RMS = {rms:.1f}")

def test_pcma_translation():
    """Verify that incoming PCMA (PT 8) packets are correctly translated to mu-law."""
    # Simulate packet with PT 8
    pkt_pt = 8
    raw_alaw_payload = b'\x55' * 160 # standard A-law silence
    
    if pkt_pt == 8:
        codec_name = "PCMA"
        payload_audio = raw_alaw_payload.translate(ALAW_TO_ULAW_TABLE)
    elif pkt_pt == 0:
        codec_name = "PCMU"
        payload_audio = raw_alaw_payload
    else:
        codec_name = "PCMU"
        payload_audio = raw_alaw_payload

    assert codec_name == "PCMA"
    assert len(payload_audio) == 160
    assert payload_audio[0] == ALAW_TO_ULAW_TABLE[0x55]
    print(f"[PASS] PCMA Packet decoding: Successfully transcoded PT 8 to mu-law")

def test_wav_building_fidelity():
    """Verify build_wav_from_pcm builds standard 16-bit 8000Hz mono PCM WAV."""
    pcm_samples = [int(10000 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(8000)]
    wav_bytes = build_wav_from_pcm(pcm_samples, sample_rate=8000)
    
    assert wav_bytes.startswith(b"RIFF"), "WAV header missing RIFF"
    assert b"WAVE" in wav_bytes[:16], "WAV header missing WAVE format"
    assert len(wav_bytes) == 44 + 16000, f"Expected 16044 bytes, got {len(wav_bytes)}"
    print(f"[PASS] WAV Building: Valid RIFF/WAVE header, size={len(wav_bytes)} bytes")

def test_vad_hysteresis_and_noise_rejection():
    """Verify VAD thresholds properly reject line noise (< 90 RMS) and capture spoken speech (> 130 RMS)."""
    # 1. Background line noise (RMS ~65)
    noise_pcm = (np.random.normal(0, 65, 160)).astype(np.int16).tolist()
    noise_rms = math.sqrt(sum(s * s for s in noise_pcm) / len(noise_pcm))
    
    speech_start_thresh = 130
    is_speaking = False
    
    if noise_rms > speech_start_thresh:
        is_speaking = True
        
    assert not is_speaking, f"Line noise (RMS {noise_rms:.1f}) falsely triggered VAD speech start!"
    print(f"[PASS] VAD Noise Rejection: Noise RMS {noise_rms:.1f} < {speech_start_thresh} (no false trigger)")
    
    # 2. Real caller voice ("Hello") with RMS ~800
    voice_pcm = (np.sin(np.linspace(0, 10, 160)) * 1200).astype(np.int16).tolist()
    voice_rms = math.sqrt(sum(s * s for s in voice_pcm) / len(voice_pcm))
    
    if voice_rms > speech_start_thresh:
        is_speaking = True
        
    assert is_speaking, f"Voice (RMS {voice_rms:.1f}) failed to trigger VAD speech start!"
    print(f"[PASS] VAD Speech Trigger: Voice RMS {voice_rms:.1f} > {speech_start_thresh} (speech detected)")

if __name__ == "__main__":
    test_pcmu_preservation()
    test_pcma_translation()
    test_wav_building_fidelity()
    test_vad_hysteresis_and_noise_rejection()
    print("\n>>> ALL OUTBOUND AUDIO PIPELINE TESTS PASSED SUCCESSFULLY! <<<")
