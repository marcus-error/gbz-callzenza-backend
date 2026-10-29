"""
AI Service (TS-017 LangGraph, TS-018 LLM).
Wraps conversational LLM calls using the unified Gemini / Mistral provider layer.
"""
from __future__ import annotations

from typing import TypedDict
from loguru import logger
from app.core.config import get_settings
from app.services.ai.llm_service import get_llm_service
from app.services.ai.prompt_service import PromptService

settings = get_settings()


class AgentState(TypedDict):
    messages: list[dict[str, str]]
    output: str


async def _call_llm(prompt: str) -> str:
    """Calls the active configured LLM provider (Gemini / Mistral / OpenAI)."""
    llm = get_llm_service()
    try:
        raw_ans = await llm.generate_response(prompt=prompt)
        return PromptService.clean_response(raw_ans)
    except Exception as exc:
        logger.error(f"[_call_llm] LLM call failed: {exc}")
        return "I am currently unable to generate a response. Please try again shortly."


async def run_agent(user_message: str) -> str:
    """Runs single-turn agent processing."""
    state: AgentState = {"messages": [{"role": "user", "content": user_message}], "output": ""}
    state["output"] = await _call_llm(user_message)
    return state["output"]
