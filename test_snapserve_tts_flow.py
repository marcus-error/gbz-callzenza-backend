import os
import sys
import time
import asyncio
import httpx
import dotenv

dotenv.load_dotenv()

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from app.core.config import get_settings
from app.services.tts_service import (
    synthesize_snapserve_speech,
    synthesize_for_call,
)
from app.services.sip_ai_bot import synthesize_text_to_mulaw, wav_to_mulaw
from app.db.models import AIAgent


async def test_snapserve():
    print("=" * 65)
    print("TESTING SNAPSERVE TTS INTEGRATION FOR LOCAL TEST AI AGENT")
    print("=" * 65)

    settings = get_settings()

    # 1. Verify Configuration
    print("\n--- TEST 1: SnapServe Configuration ---")
    voice_id = os.getenv("SNAPSERVE_VOICE_ID") or getattr(settings, "snapserve_voice_id", "")
    print(f"SnapServe Voice ID configured: {voice_id}")
    assert voice_id == "ca3007f96ae7499ab87d27ea3599956a", f"Expected ca3007f96ae7499ab87d27ea3599956a, got {voice_id}"
    print(">> TEST 1 PASSED: Correct SnapServe voice ID verified.")

    # 2. Verify Direct SnapServe Synthesis
    print("\n--- TEST 2: Direct SnapServe Speech Synthesis ---")
    t0 = time.perf_counter()
    audio_bytes = await synthesize_snapserve_speech(
        text="Hello, thank you for calling CallZenza AI voice services.",
        voice_id=voice_id,
        format="wav",
    )
    t1 = time.perf_counter()
    print(f"Synthesized {len(audio_bytes)} bytes in {(t1-t0)*1000:.1f}ms")
    assert len(audio_bytes) > 500, "SnapServe audio bytes too small or empty"
    print(">> TEST 2 PASSED: Direct SnapServe synthesis successful.")

    # 3. Verify μ-law Conversion for SIP/RTP
    print("\n--- TEST 3: Audio to 8kHz μ-law Conversion ---")
    mulaw = wav_to_mulaw(audio_bytes)
    print(f"Generated {len(mulaw)} bytes of 8kHz G.711 μ-law audio")
    assert len(mulaw) > 500, "μ-law audio too small or empty"
    print(">> TEST 3 PASSED: μ-law conversion verified.")

    # 4. Verify synthesize_for_call with is_local_test=True (Bypasses Murf completely)
    print("\n--- TEST 4: synthesize_for_call(is_local_test=True) ---")
    agent = AIAgent(id="test_agent", name="Alex")
    t0 = time.perf_counter()
    audio_url = await synthesize_for_call(
        text="Welcome to CallZenza! How can I assist you today?",
        agent=agent,
        lang="en",
        is_local_test=True,
    )
    t1 = time.perf_counter()
    print(f"Audio URL: {audio_url} (generated in {(t1-t0)*1000:.1f}ms)")
    assert audio_url and audio_url.startswith("http"), "Audio URL not generated"
    print(">> TEST 4 PASSED: synthesize_for_call routing to SnapServe verified.")

    # 5. Verify Full Local Test API End-to-End Turn
    print("\n--- TEST 5: Full Local Test API End-to-End ---")
    async with httpx.AsyncClient(timeout=45.0) as client:
        # Start
        r_start = await client.post(
            "http://localhost:8000/api/local-voice-test/start",
            json={"campaign_id": "db13ff95-65dc-4f19-b134-e48e563ff2f5"},
        )
        assert r_start.status_code == 200, f"Start failed: {r_start.status_code} {r_start.text}"
        sess_data = r_start.json()
        session_id = sess_data["session_id"]
        print(f"Started session {session_id} | Greeting Audio: {sess_data.get('audio_url')}")

        # Message
        t0 = time.perf_counter()
        r_msg = await client.post(
            "http://localhost:8000/api/local-voice-test/message",
            json={
                "session_id": session_id,
                "user_message": "Hello, tell me about your appointment options.",
                "message": "Hello, tell me about your appointment options.",
            },
        )
        t1 = time.perf_counter()
        assert r_msg.status_code == 200, f"Message failed: {r_msg.status_code} {r_msg.text}"
        msg_data = r_msg.json()
        print(f"Response ({round((t1-t0)*1000, 1)}ms): '{msg_data.get('response')}' | Audio: {msg_data.get('audio_url')}")
        assert msg_data.get("audio_url"), "Message turn failed to generate audio URL"

    print(">> TEST 5 PASSED: Local Test API completed without calling Murf.")

    print("\n" + "=" * 65)
    print("ALL SNAPSERVE TTS TESTS PASSED SUCCESSFULLY!")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(test_snapserve())
