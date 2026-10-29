"""
Mistral LLM Service Integration.
Communicates with Mistral AI API with active health checking, 429 cooldowns, and low-latency parameters.
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

MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"


class MistralService(LLMService):
    """Mistral AI LLM Service implementation."""

    def __init__(self, api_key: Optional[str] = None, default_model: Optional[str] = None):
        self.api_key = (
            api_key
            or getattr(settings, "mistral_api_key", "")
            or os.getenv("MISTRAL_API_KEY", "")
        ).strip()
        self.default_model = (
            default_model
            or getattr(settings, "mistral_model", "")
            or os.getenv("MISTRAL_MODEL", "open-mistral-7b")
        ).strip()

    async def generate_response(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 256,
    ) -> str:
        """Generates response via LLMProviderManager with automatic failover."""
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
        max_tokens: int = 256,
    ) -> AsyncIterator[str]:
        """Streams generated tokens from Mistral AI."""
        if not prompt or not prompt.strip():
            yield "Please provide a question or message."
            return

        api_key = self.api_key
        has_key = bool(api_key and len(api_key) > 5 and not api_key.startswith("dummy_"))

        if not has_key:
            res = await self.generate_response(prompt, system_prompt, history, temperature, max_tokens)
            yield res
            return

        manager = LLMProviderManager.get_instance()
        model_name = await manager.get_working_mistral_model(api_key) or "open-mistral-7b"

        messages: List[Dict[str, str]] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        if history:
            for turn in history[-4:]:
                role = turn.get("role") or turn.get("sender") or "user"
                content_text = turn.get("content", "").strip()
                if not content_text:
                    continue
                mistral_role = "assistant" if role in ("assistant", "model", "bot", "agent") else "user"
                messages.append({"role": mistral_role, "content": content_text})
        messages.append({"role": "user", "content": prompt.strip()})

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        client = manager.get_client()
        try:
            async with client.stream("POST", MISTRAL_API_URL, headers=headers, json=payload, timeout=3.0) as resp:
                if resp.status_code != 200:
                    res = await self.generate_response(prompt, system_prompt, history, temperature, max_tokens)
                    yield res
                    return

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except Exception:
                            continue
        except Exception as exc:
            logger.debug(f"[Mistral Stream] Stream error: {exc}. Falling back to standard response.")
            res = await self.generate_response(prompt, system_prompt, history, temperature, max_tokens)
            yield res
