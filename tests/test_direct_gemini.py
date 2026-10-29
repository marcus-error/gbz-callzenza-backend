import asyncio
import time
import httpx
from app.core.config import get_settings

async def main():
    settings = get_settings()
    key = settings.gemini_api_key
    model = "gemini-flash-lite-latest"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "The customer asked: what are your hours? Reply in 1 sentence."}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 60}
    }
    for i in range(3):
        t0 = time.perf_counter()
        async with httpx.AsyncClient() as client:
            resp = await client.post(url, json=payload, timeout=5.0)
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"Run {i+1}: '{text}' in {time.perf_counter() - t0:.3f}s")

if __name__ == "__main__":
    asyncio.run(main())
