import asyncio
import httpx
from app.core.config import get_settings

async def main():
    settings = get_settings()
    key = settings.gemini_api_key
    model = "gemini-flash-lite-latest"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "Hello"}]}]
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=payload, timeout=10.0)
        print("Status:", resp.status_code)
        print("Body:", resp.text[:300])

if __name__ == "__main__":
    asyncio.run(main())
