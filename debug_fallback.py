import sys
import os
import re
sys.path.insert(0, os.path.abspath("."))

from app.services.ai.llm_provider_manager import LLMProviderManager
from app.services.ai.prompt_service import PromptService

prompt = "enna venum unaku"
sys_prompt = PromptService.build_system_prompt(
    agent_name="MAGIC",
    business_name="Home Solar Experts",
    agent_persona="Helpful advisor",
    retrieved_context="",
    known_entities={},
    detected_language="ta",
    detected_intent="general_query",
    current_step="Agent",
    call_direction="outbound",
    has_relevant_context=False,
    script_full_text="[Agent: MAGIC]\n[Company: Home Solar Experts]\n[Purpose: Share solar energy savings]",
    step_instructions="[Step 1: Check solar interest]",
    script_action_instruction="Answer customer question based on knowledge base",
    qualification_state={},
    missing_fields=["agent"],
    current_stage="Agent",
    next_field="agent",
)

rag_match = re.search(r'ASSIGNED AGENT KNOWLEDGE CHUNKS:\s*\n([^\n\r]+(?:\n[^\n\r]+){0,3})', sys_prompt, re.IGNORECASE)
print(f"rag_match: {rag_match.groups() if rag_match else None}")

fb = LLMProviderManager.get_instance().derive_script_grounded_fallback(prompt, sys_prompt)
print(f"DERIVED FALLBACK: {fb!r}")
