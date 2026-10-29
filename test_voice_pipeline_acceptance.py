import asyncio
import uuid
import os
import sys

# Ensure backend app is importable
sys.path.insert(0, os.path.abspath("."))

from loguru import logger
from app.db.database import AsyncSessionLocal
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService


async def test_acceptance_flow():
    print("=" * 60)
    print("RUNNING ACCEPTANCE TEST: CALLZENZA VOICE AI PIPELINE RESET")
    print("=" * 60)

    # 1. Test Session Isolation: Call A vs Call B
    agent_id = "test_agent_123"
    session_id_A = f"call_A_{uuid.uuid4().hex[:8]}"
    session_id_B = f"call_B_{uuid.uuid4().hex[:8]}"

    # Initialize Session A
    state_A = ConversationMemoryService.init_new_session(agent_id, session_id_A, "org_1")
    ConversationMemoryService.add_turn(agent_id, session_id_A, role="assistant", content="Hello, this is MAGIC from Home Solar Experts. How are you today?")
    
    # Initialize Session B
    state_B = ConversationMemoryService.init_new_session(agent_id, session_id_B, "org_1")
    
    hist_A = ConversationMemoryService.get_history(agent_id, session_id_A)
    hist_B = ConversationMemoryService.get_history(agent_id, session_id_B)
    
    assert len(hist_A) == 1, f"Expected 1 history item in Session A, got {len(hist_A)}"
    assert len(hist_B) == 0, f"Expected 0 history items in Session B, got {len(hist_B)}"
    print("[PASS] SESSION ISOLATION: Session A and Session B are completely isolated.")

    # 2. Test Multi-Turn Dynamic Conversation Flow (3 Customer Turns)
    test_session = f"call_acceptance_{uuid.uuid4().hex[:8]}"
    ConversationMemoryService.init_new_session(agent_id, test_session, "org_1")
    greeting_text = "Hello, this is MAGIC from Home Solar Experts. How are you today?"
    ConversationMemoryService.add_turn(agent_id, test_session, role="assistant", content=greeting_text)

    customer_inputs = [
        "hello",
        "enna venum unaku",
        "solar pathi enna solla pora?"
    ]

    responses = []

    async with AsyncSessionLocal() as db:
        for idx, customer_text in enumerate(customer_inputs, 1):
            print(f"\n--- TURN {idx} ---")
            print(f"Customer: \"{customer_text}\"")

            res = await AgentEngine.process_user_query(
                db=db,
                agent_id=agent_id,
                message=customer_text,
                session_id=test_session,
                is_voice=True,
                current_language="ta",
                script_text="[Agent: MAGIC]\n[Company: Home Solar Experts]\n[Purpose: Share solar energy savings and assist with installation]\n[Step 1: Check solar interest]"
            )

            ai_resp = res.response
            safe_print = ai_resp.encode("utf-8", errors="replace").decode("utf-8")
            logger.info(f"AI Agent: \"{safe_print}\"")
            logger.info(f"Language: {res.language}")
            responses.append(ai_resp)
            await asyncio.sleep(1.0)

            # Assert response is NOT the hardcoded initial greeting repetition
            assert "I am MAGIC calling from Home solar experts regarding sharing information" not in ai_resp, \
                f"FAILED: Bot repeated hardcoded fallback greeting on turn {idx}!"

    # Verify all 3 responses are distinct
    assert len(set(responses)) == 3, f"FAILED: Expected 3 distinct responses, got {len(set(responses))}: {responses}"

    print("\n" + "=" * 60)
    print("ACCEPTANCE TEST PASSED 100%! ALL 3 TURNS PRODUCED DISTINCT DYNAMIC RESPONSES!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_acceptance_flow())
