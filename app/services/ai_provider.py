"""
AI Provider Interface and Implementations.
Exposes unified providers (Gemini, Mistral, OpenAI) without hardcoded business answers or Ollama dependencies.
"""
from __future__ import annotations

import os
import re
import time
import asyncio
import httpx
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from loguru import logger

from app.core.config import get_settings
from app.services.ai.llm_service import LLMService, get_llm_service
from app.services.ai.gemini_service import GeminiService
from app.services.ai.mistral_service import MistralService

settings = get_settings()


class AIProvider(ABC):
    @abstractmethod
    async def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        """Generates a text response based on the prompt and system prompt context."""
        pass


class GeminiProvider(AIProvider):
    """Gemini AI Provider wrapping GeminiService."""

    def __init__(self):
        self._service = GeminiService()

    async def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        res = await self._service.generate_response(prompt=prompt, system_prompt=system_prompt)
        if not res or res.startswith("I apologize, but I am unable"):
            # If API call failed, generate clean conversational fallback
            return self._generate_local_fallback(prompt)
        return res

    def _generate_local_fallback(self, prompt: str) -> str:
        """
        Generic conversational fallback when LLM API connection is temporarily unavailable.
        No hardcoded business facts are included here.
        """
        clean_prompt = prompt.strip().lower()

        # Greetings
        greetings = {"hi", "hello", "hey", "greetings", "hai", "vanakkam", "namaste"}
        words = set(re.findall(r'\b\w+\b', clean_prompt))
        if words.intersection(greetings) and len(words) <= 3:
            return "Hello! How can I assist you today?"

        if "how are you" in clean_prompt or "epdi iruka" in clean_prompt or "kaise ho" in clean_prompt:
            return "I am doing great, thank you! How can I help you today?"

        if any(w in words for w in ["thanks", "thank", "appreciate"]):
            return "You're very welcome! Let me know if you need any further information."

        if any(w in words for w in ["bye", "goodbye"]):
            return "Thank you for your time. Have a great day!"

        # Extract answer from RAG context if present in prompt
        if "Retrieved Knowledge Base Context:" in prompt or "ASSIGNED AGENT KNOWLEDGE" in prompt:
            context_part = ""
            if "Retrieved Knowledge Base Context:" in prompt:
                context_part = prompt.split("Retrieved Knowledge Base Context:")[-1].split("User Question:")[0].strip()
            elif "ASSIGNED AGENT KNOWLEDGE" in prompt:
                context_part = prompt.split("ASSIGNED AGENT KNOWLEDGE")[-1].split("CRITICAL OPERATIONAL")[0].strip()

            if context_part and "No matching information" not in context_part and "No relevant context" not in context_part:
                clean_lines = [
                    l.strip() for l in context_part.split("\n")
                    if l.strip() and not l.startswith("Score:") and not l.startswith("---") and not l.startswith("Context chunk:")
                ]
                if clean_lines:
                    return " ".join(clean_lines[:4])

        return "I am here to help you with information based on the assigned knowledge base. Please let me know how I can assist you."


class MistralProvider(AIProvider):
    """Mistral AI Provider wrapping MistralService."""

    def __init__(self):
        self._service = MistralService()

    async def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        res = await self._service.generate_response(prompt=prompt, system_prompt=system_prompt)
        if not res or res.startswith("I apologize, but I am unable"):
            fallback = GeminiProvider()
            return fallback._generate_local_fallback(prompt)
        return res


class OpenAIProvider(AIProvider):
    """OpenAI Provider."""

    async def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        api_key = (getattr(settings, "openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")).strip()
        if not api_key or len(api_key) < 5:
            provider = GeminiProvider()
            return await provider.generate_response(prompt, system_prompt)

        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        for model_name in ["gpt-4o-mini", "gpt-4o", "gpt-3.5-turbo"]:
            try:
                async with httpx.AsyncClient(timeout=25.0) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json={"model": model_name, "messages": messages, "temperature": 0.2},
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            return choices[0]["message"]["content"].strip()
            except Exception as exc:
                logger.warning(f"OpenAI API call error for model {model_name}: {exc}")
                continue

        provider = GeminiProvider()
        return provider._generate_local_fallback(prompt)


class StubProvider(AIProvider):
    async def generate_response(self, prompt: str, system_prompt: str | None = None) -> str:
        provider = GeminiProvider()
        return provider._generate_local_fallback(prompt)


def get_ai_provider() -> AIProvider:
    """Returns configured AIProvider (Gemini, Mistral, OpenAI)."""
    provider_name = (
        getattr(settings, "llm_provider", "")
        or os.getenv("LLM_PROVIDER", "gemini")
    ).strip().lower()

    if provider_name == "mistral":
        return MistralProvider()
    elif provider_name == "openai":
        return OpenAIProvider()
    elif provider_name == "gemini":
        return GeminiProvider()

    # Default to Gemini
    return GeminiProvider()
