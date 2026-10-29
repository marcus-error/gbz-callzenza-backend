import asyncio
import time
from app.services.tts_service import synthesize_murf_speech

async def main():
    s1 = "Certainly, I can help you with your account today."
    full = "Certainly, I can help you with your account today. Your current outstanding balance is zero dollars. Would you like me to email you the full statement right now?"
    
    t0 = time.perf_counter()
    b1 = await synthesize_murf_speech(s1, audio_format="WAV", sample_rate=8000)
    dur1 = time.perf_counter() - t0
    print(f"Sentence 1 (len={len(s1)}): {len(b1)} bytes in {dur1:.3f}s")
    
    t0 = time.perf_counter()
    b_full = await synthesize_murf_speech(full, audio_format="WAV", sample_rate=8000)
    dur_full = time.perf_counter() - t0
    print(f"Full response (len={len(full)}): {len(b_full)} bytes in {dur_full:.3f}s")

if __name__ == "__main__":
    asyncio.run(main())
