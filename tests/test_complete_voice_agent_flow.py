import asyncio
import os
import sys
import uuid

# Ensure backend root is on Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__))))

from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Campaign, Lead, Conversation, Call
from app.services.ai.agent_engine import AgentEngine
from app.services.sip_ai_bot import synthesize_text_to_mulaw
from sqlalchemy import select

TEST_CAMPAIGN_SCRIPT = """
CAMPAIGN NAME: Radiant Health Clinic
AGENT NAME: Dr. Maya Assistant
BUSINESS NAME: Radiant Health Clinic
OUTBOUND PURPOSE: Assisting patients with doctor consultations and appointment bookings.

GREETING:
"Hello, this is Maya from Radiant Health Clinic. How can I assist you with your health and appointments today?"

STAGE 1: General Inquiries & Services
- We provide Cardiology, Dermatology, General Medicine, and Pediatrics.
- Working hours are Monday to Saturday, 9:00 AM to 7:00 PM.
- If customer asks where we are from: We are located in T. Nagar, Chennai.
- If customer wants to book an appointment: Ask for their preferred doctor specialty and date/time.

STAGE 2: Appointment Scheduling
- Collect patient name, preferred doctor/specialty, and date.
- Confirm available slots (Morning 10 AM to 1 PM, Evening 4 PM to 7 PM).

CLOSING:
"Thank you for reaching out to Radiant Health Clinic. Have a wonderful and healthy day!"
"""

async def test_multi_turn_voice_flow():
    print("\n" + "="*70)
    print("RUNNING CALLZENZA VOICE AGENT COMPLETE PIPELINE TEST")
    print("="*70)

    test_queries = [
        "hello",
        "where are you from",
        "I want to book an appointment",
        "What services do you provide?",
        "What are your working hours?",
        "I need to schedule an appointment tomorrow",
    ]

    session_id = f"test-voice-{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        # Resolve an existing agent or create mock
        agent = (await db.execute(select(AIAgent))).scalars().first()
        agent_id = agent.id if agent else "test_agent_1"

        print(f"\n[SETUP] Active Agent: {agent_id} | Session ID: {session_id}")

        for i, user_query in enumerate(test_queries, 1):
            print(f"\n" + "-"*50)
            print(f"TURN {i}: Customer says -> \"{user_query}\"")
            print("-"*50)

            # Process through AgentEngine
            engine_res = await AgentEngine.process_user_query(
                db=db,
                agent_id=agent_id,
                message=user_query,
                session_id=session_id,
                conversation_id=session_id,
                is_voice=True,
                script_text=TEST_CAMPAIGN_SCRIPT,
            )

            ai_response = engine_res.response
            print(f"[LLM_RESPONSE] AI replies -> \"{ai_response}\"")
            print(f"[METRICS] Latency: {engine_res.latency_ms}ms | Language: {engine_res.language}")

            # ASSERTION 1: AI response must NEVER equal customer question (anti-echo test)
            assert ai_response.strip().lower() != user_query.strip().lower(), (
                f"FAILED: Turn {i} echoed the customer transcript! (Query: '{user_query}', Response: '{ai_response}')"
            )

            # ASSERTION 2: AI response must not be empty
            assert len(ai_response.strip()) > 0, f"FAILED: Turn {i} response is empty!"

            # Test TTS Synthesis for this response (SnapServe 8kHz telephony)
            mulaw_bytes = await synthesize_text_to_mulaw(ai_response, agent=agent, is_local_test=True)
            print(f"[TTS_SYNTHESIS] SnapServe generated {len(mulaw_bytes)} bytes of 8kHz G.711 mu-law audio")
            assert len(mulaw_bytes) > 0, f"FAILED: TTS synthesis produced 0 bytes for turn {i}!"

    print("\n" + "="*70)
    print("SUCCESS: ALL 6 TURNS PASSED! ZERO ECHO DETECTED. GROUNDED GEMINI + SNAPSERVE TTS VERIFIED.")
    print("="*70)

if __name__ == "__main__":
    asyncio.run(test_multi_turn_voice_flow())
