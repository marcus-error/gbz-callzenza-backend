import os
import requests
import dotenv

dotenv.load_dotenv()
key = os.getenv("GEMINI_API_KEY")
models = [
    "gemini-3.8-flash",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3-flash-preview",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gemma-4-31b-it",
]

payload = {"contents": [{"parts": [{"text": "Reply with one word: ready"}]}]}

for m in models:
    try:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent?key={key}",
            json=payload,
            timeout=4,
        )
        if r.status_code == 200:
            txt = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
            print(f"[OK] {m} -> {txt}", flush=True)
        else:
            err = r.json().get("error", {}).get("message", r.text[:80])
            print(f"[{r.status_code}] {m} -> {err}", flush=True)
    except Exception as e:
        print(f"[ERR] {m} -> {e}", flush=True)
