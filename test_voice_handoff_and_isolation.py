import asyncio
import os
import sys

# Ensure backend directory is on sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.stdout.reconfigure(encoding='utf-8')

from app.services.ai.script_engine import ScriptNormalizer
from app.services.ai.qualification_service import QualificationStageEngine
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.ai.agent_engine import AgentEngine
from app.services.ai.response_sanitizer import ResponseSanitizer

SAMPLE_OUTBOUND_SCRIPT = """
Campaign: CALL ZENZA Voice Automation Outreach
Agent: Magic
Business: CALL ZENZA
Purpose: Calling regarding your inquiry on AI voice agent solutions for customer support.

Step 1: Check Need
Question: Are you currently looking to automate your inbound and outbound calls?
If yes: Ask about daily call volume (Step 2)
If no: Ask if they would like a brochure sent via email (Step 3)

Step 2: Call Volume
Question: What is your estimated daily call volume?
"""

async def run_tests():
    print("==================================================")
    print("TEST 1: Dynamic Outbound Greeting Generation")
    print("==================================================")
    norm = ScriptNormalizer.parse_script(SAMPLE_OUTBOUND_SCRIPT, agent_name="Magic", business_name="CALL ZENZA")
    
    lead_name = "Murugan"
    agent_name = "Magic"
    biz_name = "CALL ZENZA"
    first_step_q = norm.get_current_step_question(0) or ""
    purpose = norm.outbound_purpose or norm.objective or ""
    if purpose and not purpose.endswith((".", "!", "?")):
        purpose += "."

    outbound_greeting = f"Hello {lead_name}, this is {agent_name} from {biz_name}. {purpose} {first_step_q}"
    outbound_greeting = ResponseSanitizer.clean_customer_facing_text(outbound_greeting, agent_name=agent_name, lead_name=lead_name)
    
    print(f"Generated Outbound Greeting:\n'{outbound_greeting}'")
    assert "how can i help you" not in outbound_greeting.lower(), "FAILED: Inbound phrase present in outbound greeting"
    assert "CALL ZENZA" in outbound_greeting, "FAILED: Company name missing"
    assert "Magic" in outbound_greeting, "FAILED: Agent name missing"
    assert "automate" in outbound_greeting.lower(), "FAILED: Step 1 question missing"
    print(">>> TEST 1 PASSED: Outbound greeting introduces agent, organization, states purpose, and asks Step 1 question!\n")

    print("==================================================")
    print("TEST 2: Session Isolation & Language Switch Continuity")
    print("==================================================")
    session_id = "test_session_phone_001"
    agent_id = "agent_magic_zenza"
    
    ConversationMemoryService.clear_session(agent_id, session_id)
    ConversationMemoryService.set_call_direction(agent_id, session_id, "outbound")
    ConversationMemoryService.add_turn(agent_id, session_id, role="assistant", content=outbound_greeting)

    # Customer speaks from phone in English: "Hello"
    print("\n--- TURN 1: Customer speaks 'Hello' ---")
    res1 = await AgentEngine.process_user_query(
        db=None,
        agent_id=agent_id,
        message="Hello",
        session_id=session_id,
        is_voice=True,
        script_text=SAMPLE_OUTBOUND_SCRIPT,
        call_direction="outbound"
    )
    print(f"AI Response 1: '{res1.response}'")
    assert "how can i help you" not in res1.response.lower()

    # Customer speaks from phone in Tamil: "தமிழ்ல பேசுங்க"
    print("\n--- TURN 2: Customer speaks 'தமிழ்ல பேசுங்க' ---")
    res2 = await AgentEngine.process_user_query(
        db=None,
        agent_id=agent_id,
        message="தமிழ்ல பேசுங்க",
        session_id=session_id,
        is_voice=True,
        script_text=SAMPLE_OUTBOUND_SCRIPT,
        call_direction="outbound"
    )
    print(f"AI Response 2: '{res2.response}'")
    print(f"Detected Language: {res2.language}")
    assert res2.language == "ta", "FAILED: Language did not switch to Tamil"
    assert "எப்படி உதவலாம்" not in res2.response, "FAILED: AI restarted with generic Tamil greeting"

    # Customer answers question in Tamil: "ஆமாம், ஆட்டோமேட் செய்ய வேண்டும்"
    print("\n--- TURN 3: Customer responds to qualification in Tamil ---")
    res3 = await AgentEngine.process_user_query(
        db=None,
        agent_id=agent_id,
        message="ஆமாம், ஆட்டோமேட் செய்ய வேண்டும்",
        session_id=session_id,
        is_voice=True,
        script_text=SAMPLE_OUTBOUND_SCRIPT,
        call_direction="outbound"
    )
    print(f"AI Response 3: '{res3.response}'")

    print("\n==================================================")
    print("TEST 3: Cross-Campaign Tenant Isolation ('Home Solar Experts' leak check)")
    print("==================================================")
    # Check if foreign company leaks into responses
    assert "Home Solar Experts" not in res1.response, "FAILED: Cross-campaign company leaked into Turn 1"
    assert "Home Solar Experts" not in res2.response, "FAILED: Cross-campaign company leaked into Turn 2"
    assert "Home Solar Experts" not in res3.response, "FAILED: Cross-campaign company leaked into Turn 3"
    print(">>> TEST 3 PASSED: Zero cross-campaign leakage!")
    print("All tests passed successfully!")

if __name__ == "__main__":
    asyncio.run(run_tests())
