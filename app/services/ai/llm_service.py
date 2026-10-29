"""
Unified LLM Provider Abstraction for CallZenza AI Agent.
Supports Gemini, Mistral, OpenAI, Anthropic, and other providers with a common interface.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from loguru import logger
from app.core.config import get_settings

settings = get_settings()


class LLMService(ABC):
    """Abstract interface for all LLM providers."""

    @abstractmethod
    async def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 2048,
    ) -> str:
        """
        Generates a natural conversational text response from the model.

        :param prompt: Current user query / prompt.
        :param system_prompt: Grounding instructions, agent persona, and scope rules.
        :param history: List of preceding turns [{"role": "user"|"assistant", "content": "..."}].
        :param temperature: Sampling temperature (0.0 - 1.0).
        :param max_tokens: Maximum token length for the response.
        :return: Natural language response string.
        """
        pass


def get_llm_service(provider_name: Optional[str] = None) -> LLMService:
    """
    Factory function returning the configured LLM provider service.
    Defaults to GEMINI as primary live conversational LLM.
    Mistral is reserved for input/output testing and quality evaluation.
    """
    selected = (
        provider_name
        or getattr(settings, "llm_provider", "")
        or os.getenv("LLM_PROVIDER", "gemini")
    ).strip().lower()

    if selected == "gemini":
        from app.services.ai.gemini_service import GeminiService
        return GeminiService()
    elif selected == "mistral":
        from app.services.ai.mistral_service import MistralService
        return MistralService()
    elif selected == "openai":
        from app.services.ai_provider import OpenAIProvider
        return OpenAIProvider()
    else:
        # Default to Gemini as primary live LLM
        from app.services.ai.gemini_service import GeminiService
        return GeminiService()

