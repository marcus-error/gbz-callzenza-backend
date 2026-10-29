import pytest
import asyncio
from unittest.mock import MagicMock, AsyncMock

from app.services.ai.agent_engine import AgentEngine
from app.services.ai.script_engine import ScriptNormalizer, ScriptBranchResolver
from app.services.ai.conversation.memory_service import ConversationMemoryService


@pytest.mark.asyncio
async def test_outbound_conversation_flow_no_premature_closing():
    """
    Test that caller phrases like 'hello', 'enna', 'edhuku call panna', 'enna service'
    do NOT trigger premature call closing ('Thank you for your time...').
    """
    script_text = """
    Business: Home Solar Experts
    Agent: MAGIC
    Purpose: sharing information about our residential solar program and qualifying homeowners for clean energy incentives.
    
    1. Greeting
    Hello! My name is MAGIC calling from Home Solar Experts. How are you today?
    
    2. Purpose & Service Details
    We help homeowners reduce their electric bills with government solar incentives.
    
    3. Home Ownership
    Are you the owner of your home, or are you renting?
    If yes/owner: continue to property type.
    If no/renter: not eligible.
    
    4. Property Type
    Is your home a single family house or an apartment?
    """
    
    agent_id = "test_solar_agent"
    session_id = "test_session_voice_flow_01"
    
    # 1. Test "hello"
    intent_hello = AgentEngine.classify_caller_intent("hello")
    assert intent_hello == "greeting", f"Expected 'greeting', got '{intent_hello}'"
    
    # 2. Test "enna"
    intent_enna = AgentEngine.classify_caller_intent("enna")
    assert intent_enna == "greeting", f"Expected 'greeting', got '{intent_enna}'"
    
    # 3. Test "edhuku call panna"
    intent_purpose = AgentEngine.classify_caller_intent("edhuku call panna")
    assert intent_purpose == "call_purpose", f"Expected 'call_purpose', got '{intent_purpose}'"
    
    # 4. Test "edhyuku call pasnna"
    intent_purpose2 = AgentEngine.classify_caller_intent("edhyuku call pasnna")
    assert intent_purpose2 == "call_purpose", f"Expected 'call_purpose', got '{intent_purpose2}'"
    
    # 5. Test "enna service"
    intent_service = AgentEngine.classify_caller_intent("enna service")
    assert intent_service == "service_inquiry", f"Expected 'service_inquiry', got '{intent_service}'"
    
    # Verify ScriptBranchResolver actions
    norm_script = ScriptNormalizer.parse_script(script_text, agent_name="MAGIC", business_name="Home Solar Experts")
    
    # Action for "call_purpose"
    action_purpose = ScriptBranchResolver.resolve_action(
        script=norm_script,
        current_step_index=0,
        intent="call_purpose",
        collected_entities={},
        caller_msg="edhuku call panna",
    )
    assert action_purpose["action"] == "answer_call_purpose"
    assert not action_purpose.get("advance_step", False)
    
    # Action for "greeting"
    action_greeting = ScriptBranchResolver.resolve_action(
        script=norm_script,
        current_step_index=0,
        intent="greeting",
        collected_entities={},
        caller_msg="hello",
    )
    assert action_greeting["action"] == "answer_greeting"
    
    print("[PASS] Intent classification & script branch resolution verified for Tamil/Tanglish call purpose & greeting!")


if __name__ == "__main__":
    asyncio.run(test_outbound_conversation_flow_no_premature_closing())
