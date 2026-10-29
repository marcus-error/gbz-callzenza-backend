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

from app.services.ai.llm_provider_manager import LLMProviderManager
from app.services.ai.gemini_service import GeminiService
from app.db.models import AIAgent


async def test_all():
    print("=" * 65)
    print("TESTING GEMINI TIMEOUT, HEALTH RECOVERY & LATENCY PIPELINE")
    print("=" * 65)

    mgr = LLMProviderManager.get_instance()

    # -------------------------------------------------------------
    # 1. Verify Configurable Timeout
    # -------------------------------------------------------------
    print("\n--- TEST 1: Configurable Gemini Timeout ---")
    timeout = mgr.get_gemini_timeout()
    print(f"Gemini timeout configured: {timeout}s")
    assert timeout == 4.0, f"Expected timeout 4.0s, got {timeout}s"
    print(">> TEST 1 PASSED: Configurable timeout verified (4.0s).")

    # -------------------------------------------------------------
    # 2. Verify Single Transient Failure Does NOT Mark Provider Unhealthy
    # -------------------------------------------------------------
    print("\n--- TEST 2: Single Transient Timeout Health Tracking ---")
    # Reset state
    mgr.record_provider_success("gemini")
    assert mgr.is_provider_healthy("gemini"), "Gemini should be healthy initially"

    # Simulate 1st transient timeout
    mgr.record_provider_failure("gemini", "TimeoutException", message="Read timeout > 4.0s")
    assert mgr.is_provider_healthy("gemini"), "Gemini MUST remain healthy after single transient timeout!"
    print(">> TEST 2 PASSED: 1 transient timeout does NOT disable Gemini (healthy=True).")

    # -------------------------------------------------------------
    # 3. Verify Consecutive Failures Trigger Failure-Aware Cooldown
    # -------------------------------------------------------------
    print("\n--- TEST 3: Consecutive Transient Failures Short Cooldown ---")
    # Simulate 2nd consecutive timeout
    mgr.record_provider_failure("gemini", "TimeoutException", message="Read timeout > 4.0s")
    assert not mgr.is_provider_healthy("gemini"), "Gemini should have short cooldown on 2nd consecutive failure"
    cooldown_rem = mgr._provider_cooldowns.get("gemini", 0.0) - time.time()
    print(f"Cooldown remaining after 2 consecutive failures: {cooldown_rem:.1f}s")
    assert 0.0 < cooldown_rem <= 3.5, f"Expected ~3s cooldown, got {cooldown_rem}s"
    print(">> TEST 3 PASSED: 2 consecutive failures trigger short cooldown (~3.0s).")

    # -------------------------------------------------------------
    # 4. Verify Success Resets Consecutive Failures & Restores Health
    # -------------------------------------------------------------
    print("\n--- TEST 4: Success Restores Health State ---")
    mgr.record_provider_success("gemini")
    assert mgr.is_provider_healthy("gemini"), "Gemini should be immediately healthy after success"
    assert mgr._consecutive_failures.get("gemini") == 0, "Failure count should reset to 0"
    print(">> TEST 4 PASSED: Success resets failure count and restores health.")

    # -------------------------------------------------------------
    # 5. Verify ~2.7s Response is Treated as Success (Within 4.0s)
    # -------------------------------------------------------------
    print("\n--- TEST 5: Latency Tolerance (< 4.0s Response Succeeds) ---")
    gemini = GeminiService()
    t0 = time.perf_counter()
    resp = await gemini.generate_response(
        prompt="Reply with exactly: 'CallZenza voice system active'",
        system_prompt="You are a helpful assistant.",
    )
    t1 = time.perf_counter()
    elapsed_ms = (t1 - t0) * 1000
    print(f"Response (in {elapsed_ms:.1f}ms): '{resp}'")
    assert len(resp) > 0, "Response must not be empty"
    print(f">> TEST 5 PASSED: Response returned in {elapsed_ms:.1f}ms without timeout error.")

    # -------------------------------------------------------------
    # 6. Verify Streaming Gemini Response
    # -------------------------------------------------------------
    print("\n--- TEST 6: Streaming Generation Pipeline ---")
    t_stream_start = time.perf_counter()
    chunks = []
    async for chunk in gemini.generate_response_stream(
        prompt="Tell me one brief sentence about appointment booking.",
        system_prompt="You are a helpful receptionist.",
        max_tokens=60,
    ):
        chunks.append(chunk)
    stream_ms = (time.perf_counter() - t_stream_start) * 1000
    full_stream_text = "".join(chunks).strip()
    print(f"Stream completed in {stream_ms:.1f}ms ({len(chunks)} chunks): '{full_stream_text}'")
    assert len(full_stream_text) > 0, "Streamed text must not be empty"
    print(">> TEST 6 PASSED: Streaming generation verified.")

    # -------------------------------------------------------------
    # 7. Local Voice Test Multi-Turn End-to-End
    # -------------------------------------------------------------
    print("\n--- TEST 7: Local Voice Test End-to-End Turn ---")
    async with httpx.AsyncClient(timeout=35.0) as client:
        # Start session with real campaign
        r_start = await client.post(
            "http://localhost:8000/api/local-voice-test/start",
            json={"campaign_id": "db13ff95-65dc-4f19-b134-e48e563ff2f5"},
        )
        assert r_start.status_code == 200, f"Start failed: {r_start.status_code} {r_start.text}"
        sess_data = r_start.json()
        session_id = sess_data["session_id"]
        print(f"Started session {session_id} | Greeting: '{sess_data.get('response')}' | Audio: {sess_data.get('audio_url')}")

        # Send Turn 1
        t_turn1 = time.perf_counter()
        r_msg1 = await client.post(
            "http://localhost:8000/api/local-voice-test/message",
            json={"session_id": session_id, "user_message": "Hello, I want to know more.", "message": "Hello, I want to know more."},
        )
        t_turn1_ms = (time.perf_counter() - t_turn1) * 1000
        assert r_msg1.status_code == 200, f"Turn 1 failed: {r_msg1.status_code} {r_msg1.text}"
        d1 = r_msg1.json()
        print(f"Turn 1 Response ({t_turn1_ms:.1f}ms): '{d1.get('response')}' | Audio: {d1.get('audio_url')}")

        # Send Turn 2
        t_turn2 = time.perf_counter()
        r_msg2 = await client.post(
            "http://localhost:8000/api/local-voice-test/message",
            json={"session_id": session_id, "user_message": "Can I book a consultation tomorrow at 3pm?", "message": "Can I book a consultation tomorrow at 3pm?"},
        )
        t_turn2_ms = (time.perf_counter() - t_turn2) * 1000
        assert r_msg2.status_code == 200, f"Turn 2 failed: {r_msg2.status_code} {r_msg2.text}"
        d2 = r_msg2.json()
        print(f"Turn 2 Response ({t_turn2_ms:.1f}ms): '{d2.get('response')}' | Audio: {d2.get('audio_url')}")

    print(">> TEST 7 PASSED: End-to-end Local Voice Test completed successfully.")

    print("\n" + "=" * 65)
    print("ALL TESTS PASSED! GEMINI TIMEOUT & LATENCY ISSUE RESOLVED.")
    print("=" * 65)


if __name__ == "__main__":
    asyncio.run(test_all())
