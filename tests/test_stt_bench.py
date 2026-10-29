import asyncio
import time
from app.services.stt_service import transcribe_speech_gemini
from app.services.sip_ai_bot import build_wav_from_pcm, ulaw2pcm, synthesize_text_to_mulaw

async def main():
    print("Synthesizing test audio...")
    mulaw = await synthesize_text_to_mulaw("Hello, what are your store hours today?")
    pcm = [ulaw2pcm(b) for b in mulaw]
    wav = build_wav_from_pcm(pcm, sample_rate=8000)
    print("Testing Gemini STT...")
    t0 = time.perf_counter()
    res = await transcribe_speech_gemini(wav)
    dur = time.perf_counter() - t0
    print(f"Gemini Transcript: '{res}' (took {dur:.3f}s)")

if __name__ == "__main__":
    asyncio.run(main())
