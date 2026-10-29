import asyncio
import time
from app.services.stt_service import get_genai_client
from app.services.sip_ai_bot import build_wav_from_pcm, ulaw2pcm, synthesize_text_to_mulaw
from google.genai import types

async def main():
    print("Synthesizing audio...")
    mulaw = await synthesize_text_to_mulaw("Hello, what are your store hours today?")
    pcm = [ulaw2pcm(b) for b in mulaw]
    wav = build_wav_from_pcm(pcm, sample_rate=8000)
    
    client = get_genai_client()
    prompt = "Transcribe the spoken audio verbatim. Output ONLY the exact transcription, nothing else."
    
    for model_name in ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.8-flash"]:
        t0 = time.perf_counter()
        try:
            resp = client.models.generate_content(
                model=model_name,
                contents=[types.Part.from_bytes(data=wav, mime_type="audio/wav"), prompt]
            )
            print(f"[{model_name}] Transcript: '{resp.text.strip()}' in {time.perf_counter() - t0:.3f}s")
        except Exception as e:
            print(f"[{model_name}] Failed: {e}")

if __name__ == "__main__":
    asyncio.run(main())
