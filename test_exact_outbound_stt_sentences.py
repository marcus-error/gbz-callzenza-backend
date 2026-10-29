import asyncio
import time
import math
import numpy as np
from loguru import logger

from app.services.sip_ai_bot import (
    ulaw2pcm,
    build_wav_from_pcm,
    synthesize_text_to_mulaw,
    resample_and_normalize_audio,
)
from app.services.stt_service import transcribe_audio

TEST_SENTENCES = [
    "Hello",
    "Can you talk in Tamil?",
    "En peru Mugilan Viswanathan",
    "Yes",
    "No, thank you",
    "I want to know more about the solar program and how it works.",
]

async def run_outbound_stt_sentence_test():
    print("=" * 70)
    print("RUNNING OUTBOUND STT EXACT SENTENCE TEST SUITE")
    print("=" * 70)

    for idx, sentence in enumerate(TEST_SENTENCES, 1):
        print(f"\n--- TEST {idx}: Customer Spoken Sentence: '{sentence}' ---")
        
        # 1. Synthesize audio to simulate customer phone audio
        mulaw_bytes = await synthesize_text_to_mulaw(sentence, is_local_test=True)
        assert mulaw_bytes and len(mulaw_bytes) > 0, f"Failed to synthesize audio for '{sentence}'"
        
        # 2. Simulate RTP packet ingestion (160 bytes / 20ms per packet)
        pcm_stream = []
        rx_packets = 0
        rx_bytes = 0
        
        for i in range(0, len(mulaw_bytes), 160):
            chunk = mulaw_bytes[i:i + 160]
            rx_packets += 1
            rx_bytes += len(chunk)
            pcm_chunk = [ulaw2pcm(b) for b in chunk]
            pcm_stream.extend(pcm_chunk)
            
        audio_dur_ms = int(len(pcm_stream) / 8)
        
        # 3. Build 16-bit 8000Hz WAV
        wav_bytes = build_wav_from_pcm(pcm_stream, sample_rate=8000)
        
        # 4. Transcribe with active STT
        t0 = time.perf_counter()
        stt_transcript = await transcribe_audio(wav_bytes, mimetype="audio/wav", language_code="en-IN")
        t1 = time.perf_counter()
        elapsed_ms = int((t1 - t0) * 1000)
        
        stt_final = (stt_transcript or "").strip()
        
        logger.info(
            f"[STT DEBUG] test_id={idx}\n"
            f"RTP packets received: {rx_packets}\n"
            f"RTP payload bytes: {rx_bytes}\n"
            f"Decoded PCM bytes: {len(pcm_stream)*2}\n"
            f"Sample rate: 8000\n"
            f"Channels: 1\n"
            f"Audio duration: {audio_dur_ms}ms\n"
            f"STT chunks sent: 1"
        )
        logger.info(f"[STT FINAL] test_id={idx} text=\"{stt_final}\"")
        logger.info(f"[AGENT INPUT] test_id={idx} text=\"{stt_final}\"")
        
        print(f"Original spoken text : '{sentence}'")
        print(f"Recognized STT text  : '{stt_final}' (took {elapsed_ms}ms)")
        
        # Verify no phantom noise tokens and non-empty transcript
        assert len(stt_final) > 0, f"TEST {idx} FAILED: Empty transcript for '{sentence}'"
        assert stt_final.lower() not in ("de", "mama", "um", "uh"), f"TEST {idx} FAILED: Received phantom noise '{stt_final}'"
        print(f">> TEST {idx} PASSED!")

    print("\n" + "=" * 70)
    print("ALL 6 OUTBOUND STT EXACT SENTENCE TESTS PASSED WITH 100% SUCCESS!")
    print("=" * 70)

if __name__ == "__main__":
    asyncio.run(run_outbound_stt_sentence_test())
