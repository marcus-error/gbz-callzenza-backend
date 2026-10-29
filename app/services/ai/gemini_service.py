"""
Gemini LLM Service Integration.
Communicates with Google Gemini API for fast, natural conversational AI responses
with active model health caching and zero waterfall delays.
"""
from __future__ import annotations

import os
import json
import httpx
from typing import Any, AsyncIterator, Dict, List, Optional
from loguru import logger

from app.core.config import get_settings
from app.services.ai.llm_service import LLMService
from app.services.ai.llm_provider_manager import LLMProviderManager

settings = get_settings()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


class GeminiService(LLMService):
    """Google Gemini LLM Service implementation with zero retry waterfall."""

    def __init__(self, api_key: Optional[str] = None, default_model: Optional[str] = None):
        self.api_key = (
            api_key
            or getattr(settings, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        self.default_model = (
            default_model
            or os.getenv("GEMINI_MODEL", "")
            or getattr(settings, "gemini_model", "gemini-flash-latest")
        ).strip()

    async def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> str:
        """Generates response via LLMProviderManager ensuring sub-second response."""
        manager = LLMProviderManager.get_instance()
        return await manager.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            history=history,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def generate_response_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 512,
    ) -> AsyncIterator[str]:
        """Streams generated tokens for real-time TTS pipelining via LLMProviderManager."""
        manager = LLMProviderManager.get_instance()
        async for chunk in manager.generate_stream(
            prompt=prompt,
            system_prompt=system_prompt,
            history=history,
            temperature=temperature,
            max_tokens=max_tokens,
        ):
            yield chunk
