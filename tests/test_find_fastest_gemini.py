import asyncio
import time
import httpx
from app.core.config import get_settings

async def main():
    settings = get_settings()
    key = settings.gemini_api_key
    models = ["gemini-2.5-flash-lite", "gemini-3.5-flash-lite", "gemini-3.5-flash", "gemini-3.8-flash", "gemini-flash-latest"]
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 20}
    }
    for m in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}"
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=payload, timeout=6.0)
                if resp.status_code == 200:
                    text = resp.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
                    print(f"[{m}] SUCCESS ({time.perf_counter() - t0:.2f}s): '{text}'")
                else:
                    print(f"[{m}] Status {resp.status_code} ({time.perf_counter() - t0:.2f}s): {resp.text[:100]}")
        except Exception as e:
            print(f"[{m}] FAILED ({time.perf_counter() - t0:.2f}s): {type(e).__name__}")

if __name__ == "__main__":
    asyncio.run(main())
