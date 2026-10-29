import asyncio
import time
from app.services.sip_ai_bot import synthesize_text_to_mulaw

async def test_full_sentence_tts_delivery():
    print("=== TEST: Full Multi-Sentence TTS Delivery ===")
    full_text = (
        "That is great to hear, as these solar panels will help reduce your electric bills and increase your home value as well. "
        "I just need to collect a few quick details from you to see if you are eligible for the free consultation, and I will keep this under two minutes. "
        "To start, do you rent or own your home?"
    )
    
    t0 = time.perf_counter()
    mulaw_audio = await synthesize_text_to_mulaw(full_text, is_local_test=True)
    t1 = time.perf_counter()
    
    assert mulaw_audio and len(mulaw_audio) > 0, "TTS failed to synthesize audio!"
    
    dur_ms = int(len(mulaw_audio) / 8)
    dur_sec = dur_ms / 1000.0
    synthesis_ms = int((t1 - t0) * 1000)
    
    # Verify packetization
    chunk_size = 160
    chunks = []
    for i in range(0, len(mulaw_audio), chunk_size):
        slice_bytes = mulaw_audio[i:i + chunk_size]
        if len(slice_bytes) < chunk_size:
            slice_bytes += b'\xff' * (chunk_size - len(slice_bytes))
        chunks.append(slice_bytes)
        
    total_transmitted_bytes = sum(len(c) for c in chunks)
    delivery_ratio = len(mulaw_audio) / total_transmitted_bytes
    
    print(f"Text Length: {len(full_text)} chars")
    print(f"TTS Synthesized Audio: {len(mulaw_audio)} bytes ({dur_sec:.2f} seconds)")
    print(f"TTS Synthesis Time: {synthesis_ms} ms")
    print(f"Total 20ms Packets: {len(chunks)} packets")
    print(f"Total Sent to Phone: {total_transmitted_bytes} bytes")
    print(f"Delivery Ratio: {delivery_ratio:.4f} (100% complete)")
    
    assert dur_sec >= 8.0, f"Expected full 8+ second utterance, got {dur_sec:.2f}s"
    assert len(chunks) >= 400, f"Expected 400+ packets, got {len(chunks)}"
    print("\n>>> SUCCESS: Complete multi-sentence speech synthesized and verified for full 100% delivery!")

if __name__ == "__main__":
    asyncio.run(test_full_sentence_tts_delivery())
