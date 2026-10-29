import asyncio
import os
import sys
import time
from app.core.config import get_settings
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.sip_ai_bot import synthesize_text_to_mulaw
from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent

script_sample = """
Campaign: Home Solar Experts
Agent: Alex
Objective: Qualify residential leads for solar consultation
1. Greeting: My name is Alex and I am calling you from Home Solar Experts. How are you today?
2. Home Ownership: We provide homeowners a free consultation on zero-down solar panels. To see if you qualify, do you currently rent or own your home?
3. Property Type: Great! Do you live in a single family home or an apartment?
4. Closing: Wonderful, based on your answers you qualify for the free consultation. Can I book you for tomorrow at 2 PM?
"""

async def run_murf_voice_agent_test():
    print("=" * 60, flush=True)
    print("TESTING CALLZENZA AI AGENT WITH MURF TTS (Elvira / Falcon)", flush=True)
    print("=" * 60, flush=True)
    
    settings = get_settings()
    print(f"Active TTS Provider: {settings.tts_provider}", flush=True)
    print(f"Murf Voice ID: {settings.murf_voice_id}", flush=True)
    print(f"Murf Model: {settings.murf_model}", flush=True)
    
    session_id = f"test-murf-session-{int(time.time())}"
    agent_id = "test_agent_solar"
    ConversationMemoryService.clear_session(agent_id, session_id)
    ConversationMemoryService.set_call_direction(agent_id, session_id, "outbound")
    
    agent = AIAgent(
        id=agent_id,
        name="Alex",
        voice_id="Elvira",
        voice_model_id="Falcon",
    )
    
    # 1. Test Outbound Greeting TTS
    opening_text = "My name is Alex and I am calling you from Home Solar Experts. How are you today?"
    print(f"\n[TURN 0 - Greeting] Synthesizing greeting with Murf: '{opening_text}'", flush=True)
    t0 = time.perf_counter()
    mulaw_bytes = await synthesize_text_to_mulaw(opening_text, agent=agent, call_id=session_id, turn_id=1)
    t1 = time.perf_counter()
    print(f"-> Murf Greeting synthesized: {len(mulaw_bytes)} bytes of 8kHz G.711 mu-law audio in {(t1-t0)*1000:.1f}ms", flush=True)
    assert len(mulaw_bytes) > 2000, "Greeting audio too small"
    
    # 2. Turn 1: Customer answers "Okay."
    async with AsyncSessionLocal() as db:
        print("\n[TURN 1] Customer says: 'Okay.'", flush=True)
        res1 = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="Okay.",
            session_id=session_id,
            script_text=script_sample,
            call_direction="outbound"
        )
        print(f"-> AI Response 1: '{res1.response}'", flush=True)
        t0 = time.perf_counter()
        mulaw1 = await synthesize_text_to_mulaw(res1.response, agent=agent, call_id=session_id, turn_id=2)
        t1 = time.perf_counter()
        print(f"-> Murf Turn 1 Audio: {len(mulaw1)} bytes in {(t1-t0)*1000:.1f}ms", flush=True)
        assert len(mulaw1) > 2000, "Turn 1 audio too small"

        # 3. Turn 2: Customer answers "I rent my home."
        print("\n[TURN 2] Customer says: 'I rent my home.'", flush=True)
        res2 = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="I rent my home.",
            session_id=session_id,
            script_text=script_sample,
            call_direction="outbound"
        )
        print(f"-> AI Response 2: '{res2.response}'", flush=True)
        t0 = time.perf_counter()
        mulaw2 = await synthesize_text_to_mulaw(res2.response, agent=agent, call_id=session_id, turn_id=3)
        t1 = time.perf_counter()
        print(f"-> Murf Turn 2 Audio: {len(mulaw2)} bytes in {(t1-t0)*1000:.1f}ms", flush=True)
        assert len(mulaw2) > 2000, "Turn 2 audio too small"

    print("\n" + "=" * 60, flush=True)
    print("ALL TESTS PASSED: Murf TTS (Elvira/Falcon) completely replaces SnapServe!", flush=True)
    print("=" * 60, flush=True)

if __name__ == "__main__":
    asyncio.run(run_murf_voice_agent_test())
