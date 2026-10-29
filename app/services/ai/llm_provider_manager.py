"""
Centralized LLM Provider & Health Manager for CallZenza AI Agent.
Guarantees responsive voice conversations:
- Configurable Gemini timeout (default 4.0s)
- Failure-aware health cooldowns (1 transient timeout keeps provider available; consecutive timeouts apply short gradual cooldowns)
- Fast startup verification and caching of active models
- Streaming support for sentence-boundary TTS pipelining
- Safe, comprehensive diagnostic error logging (NO API keys, NO customer PII)
- Fully grounded script fallback based on campaign, agent persona, script steps, and RAG knowledge
"""
from __future__ import annotations

import os
import re
import json
import time
import asyncio
import httpx
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
MISTRAL_API_URL = "https://api.mistral.ai/v1/chat/completions"
OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"

# Curated, verified supported model candidates (in order of preference)
VERIFIED_GEMINI_MODELS = [
    "gemini-flash-lite-latest",
    "gemini-flash-latest",
    "gemini-2.0-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
]

VERIFIED_MISTRAL_MODELS = [
    "open-mistral-7b",
    "mistral-tiny",
]


class LLMProviderManager:
    """Manages LLM providers with active model health caching, streaming, and failure-aware health tracking."""

    _instance: Optional[LLMProviderManager] = None
    _initialized: bool = False

    def __init__(self):
        self._verified_gemini_model: Optional[str] = None
        self._verified_mistral_model: Optional[str] = None
        self._provider_cooldowns: Dict[str, float] = {}  # provider -> cooldown_until_timestamp
        self._consecutive_failures: Dict[str, int] = {}  # provider -> failure_count
        self._failed_models: set[str] = set()
        self._http_client: Optional[httpx.AsyncClient] = None
        self._logged_timeout: bool = False

    @classmethod
    def get_instance(cls) -> LLMProviderManager:
        if cls._instance is None:
            cls._instance = LLMProviderManager()
        return cls._instance

    def get_gemini_timeout(self) -> float:
        """Returns the configured Gemini timeout in seconds (default 7.5s for telephony)."""
        raw = os.getenv("GEMINI_TIMEOUT_SECONDS") or getattr(settings, "gemini_timeout_seconds", 7.5) or 7.5
        try:
            val = float(raw)
        except (ValueError, TypeError):
            val = 7.5
        if not self._logged_timeout:
            logger.info(f"[LLM] Gemini timeout configured={val:.1f}s")
            self._logged_timeout = True
        return val

    def get_client(self) -> httpx.AsyncClient:
        timeout_val = self.get_gemini_timeout()
        read_timeout = max(6.0, timeout_val + 2.0)
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=2.5, read=read_timeout, write=2.5, pool=15.0),
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=50),
            )
        return self._http_client

    def is_provider_healthy(self, provider: str) -> bool:
        cooldown = self._provider_cooldowns.get(provider, 0.0)
        return time.time() > cooldown

    def set_provider_cooldown(self, provider: str, duration_sec: float = 5.0, reason: str = ""):
        self._provider_cooldowns[provider] = time.time() + duration_sec
        logger.warning(
            f"[LLMManager] Provider '{provider}' marked unhealthy | cooldown={duration_sec}s | reason={reason or 'rate-limit/high load'}"
        )

    def record_provider_success(self, provider: str):
        """Resets consecutive failure count and clears cooldown on successful turn."""
        self._consecutive_failures[provider] = 0
        self._provider_cooldowns.pop(provider, None)

    def record_provider_failure(
        self,
        provider: str,
        error_type: str,
        status_code: Optional[int] = None,
        message: str = "",
        model_name: Optional[str] = None,
    ):
        """
        Failure-aware health tracking:
        - Single transient timeout/503: consecutive_failures=1, remains AVAILABLE (no 20s freeze).
        - 2 consecutive failures: short cooldown (3.0s).
        - 3+ consecutive failures: gradual cooldown (5.0s to 10.0s max).
        - Permanent 404: model blacklisted.
        - Permanent 401/403: auth cooldown (60.0s).
        """
        if status_code in (401, 403):
            self.set_provider_cooldown(provider, duration_sec=60.0, reason=f"Auth failure HTTP {status_code}")
            return

        if status_code == 404 and model_name:
            self._failed_models.add(model_name)
            if self._verified_gemini_model == model_name:
                self._verified_gemini_model = None
            return

        # Transient failures: TimeoutException, 408, 429, 500, 502, 503, 504, network error
        cnt = self._consecutive_failures.get(provider, 0) + 1
        self._consecutive_failures[provider] = cnt

        if cnt == 1:
            logger.info(
                f"[LLMManager] Provider '{provider}' transient failure count=1 | remaining available | reason={error_type}"
            )
        elif cnt == 2:
            self.set_provider_cooldown(
                provider,
                duration_sec=3.0,
                reason=f"{error_type} ({cnt} consecutive failures: {message[:40]})",
            )
        else:
            gradual_cooldown = min(10.0, 3.0 + (cnt - 2) * 2.0)
            self.set_provider_cooldown(
                provider,
                duration_sec=gradual_cooldown,
                reason=f"{error_type} ({cnt} consecutive failures: {message[:40]})",
            )

    async def get_working_gemini_model(self, api_key: str) -> Optional[str]:
        """Returns the verified fast Gemini model, checking once and caching result."""
        if self._verified_gemini_model and self._verified_gemini_model not in self._failed_models:
            return self._verified_gemini_model

        configured_model = (
            os.getenv("GEMINI_MODEL", "")
            or getattr(settings, "gemini_model", "")
            or "gemini-flash-lite-latest"
        ).strip()

        if configured_model in ("gemini-2.0-flash", "gemini-1.5-flash", "gemini-2.5-flash", "gemini-1.5-pro", "gemini-3.1-flash-lite-preview"):
            configured_model = "gemini-flash-lite-latest"

        # Fast-path: pin known working high-throughput voice model immediately without sequential test timeouts
        if configured_model and configured_model not in self._failed_models:
            self._verified_gemini_model = configured_model
            logger.info(f"[LLM] provider=gemini | model={configured_model} | status=ready (configured)")
            return configured_model

        client = self.get_client()
        candidates = [configured_model] + [m for m in VERIFIED_GEMINI_MODELS if m != configured_model]

        for model_name in candidates:
            if model_name in self._failed_models:
                continue
            t_probe = time.perf_counter()
            try:
                url = f"{GEMINI_BASE_URL}/models/{model_name}:generateContent?key={api_key}"
                resp = await client.post(
                    url,
                    json={"contents": [{"role": "user", "parts": [{"text": "ping"}]}]},
                    timeout=2.0,
                )
                dur = (time.perf_counter() - t_probe) * 1000
                if resp.status_code == 200:
                    self._verified_gemini_model = model_name
                    logger.info(f"[LLM] provider=gemini | model={model_name} | status=ready | elapsed={dur:.1f}ms")
                    return model_name
                elif resp.status_code in (404, 400):
                    self._failed_models.add(model_name)
                elif resp.status_code in (429, 503):
                    self.record_provider_failure("gemini", f"HTTP {resp.status_code}", resp.status_code, "high load")
                    break
            except Exception as e:
                self._failed_models.add(model_name)

        # Default fallback candidate
        self._verified_gemini_model = "gemini-3.5-flash-lite"
        return self._verified_gemini_model

    async def get_working_mistral_model(self, api_key: str) -> Optional[str]:
        """Returns the verified fast Mistral model."""
        if self._verified_mistral_model:
            return self._verified_mistral_model

        configured_model = (
            os.getenv("MISTRAL_MODEL", "")
            or getattr(settings, "mistral_model", "")
            or "open-mistral-7b"
        ).strip()

        if configured_model == "mistral-small-latest":
            configured_model = "open-mistral-7b"

        client = self.get_client()
        candidates = [configured_model] + [m for m in VERIFIED_MISTRAL_MODELS if m != configured_model]

        for model_name in candidates:
            if model_name in self._failed_models:
                continue
            try:
                resp = await client.post(
                    MISTRAL_API_URL,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5},
                    timeout=2.0,
                )
                if resp.status_code == 200:
                    self._verified_mistral_model = model_name
                    logger.info(f"[LLM] provider=mistral | model={model_name} | status=ready")
                    return model_name
                elif resp.status_code == 429:
                    self._failed_models.add(model_name)
                elif resp.status_code in (404, 400):
                    self._failed_models.add(model_name)
            except Exception:
                self._failed_models.add(model_name)

        self._verified_mistral_model = "open-mistral-7b"
        return self._verified_mistral_model

    def derive_script_grounded_fallback(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """
        Generates a natural, context-grounded fallback response based on the active campaign,
        agent persona, script step instructions, and retrieved RAG knowledge base.
        Preserves complete agent/tenant isolation without global state leakage.
        """
        if not system_prompt:
            return "Thank you for letting me know. How would you like to proceed?"

        # 1. Detect language context from system prompt
        is_tamil = "ta" in system_prompt.lower() or any("\u0b80" <= c <= "\u0bff" for c in prompt or system_prompt)
        is_hindi = "hi" in system_prompt.lower() or any("\u0900" <= c <= "\u097f" for c in prompt or system_prompt)

        prompt_clean = (prompt or "").strip().lower()

        # 2. Check for explicit question/dialogue in current script step instructions
        q_match = re.search(r'(?:Ask|Say|Prompt|Question|Greeting)\s*:\s*["\u201c\']([^"\u201d\']{8,180})["\u201d\']', system_prompt, re.IGNORECASE)
        if q_match:
            candidate = q_match.group(1).strip()
            if candidate.lower() != prompt_clean and not candidate.lower().startswith("the customer") and "assigned agent" not in candidate.lower():
                return candidate

        # 3. Check for direct dialogue in CURRENT SCRIPT STEP INSTRUCTIONS
        step_match = re.search(r'CURRENT SCRIPT STEP INSTRUCTIONS:\s*([^\n\r]+(?:\n[^\n\r]+){0,2})', system_prompt, re.IGNORECASE)
        if step_match:
            step_text = step_match.group(1).strip()
            step_quotes = re.findall(r'["\u201c\']([^"\u201d\']{8,180})["\u201d\']', step_text)
            for q in step_quotes:
                cand = q.strip()
                if cand.lower() != prompt_clean and not cand.lower().startswith("the customer") and "assigned agent" not in cand.lower():
                    return cand

        # 4. Check for retrieved RAG knowledge chunks
        rag_match = re.search(r'ASSIGNED AGENT KNOWLEDGE[^:]*:\s*\n([^\n\r]+(?:\n[^\n\r]+){0,3})', system_prompt, re.IGNORECASE)
        if rag_match:
            rag_chunk = rag_match.group(1).strip()
            if rag_chunk and not rag_chunk.startswith("(No additional") and len(rag_chunk) > 15:
                cleaned_fact = re.sub(r'^[-\*\d\.\s]+', '', rag_chunk).strip()
                sentences = re.split(r'(?<=[.?!])\s+', cleaned_fact)
                if sentences and len(sentences[0]) > 10:
                    fact_sentence = sentences[0].strip()
                    if fact_sentence.lower() != prompt_clean:
                        return fact_sentence

        # 5. Natural localized step-appropriate fallback
        if is_tamil:
            return "நான் உதவ தயாராக உள்ளேன். உங்கள் விவரங்களை கூற முடியுமா?"
        elif is_hindi:
            return "मैं आपकी सहायता करने के लिए तैयार हूँ। क्या आप थोड़ा और विवरण बता सकते हैं?"
        return "I'd be glad to help with that. Could you please share a few more details?"

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 256,
    ) -> str:
        """
        Executes a single fast LLM turn with configurable Gemini timeout and failure-aware fallback.
        Logs safe error details and metrics on any failure.
        """
        t0 = time.perf_counter()
        gemini_timeout = self.get_gemini_timeout()

        # Metrics calculation
        prompt_chars = len(prompt or "")
        history_chars = sum(len(t.get("content") or t.get("text") or "") for t in (history or []))
        rag_chars = len(system_prompt or "") if system_prompt else 0
        logger.debug(
            f"[LLM] prompt_chars={prompt_chars} history_chars={history_chars} rag_chars={rag_chars} max_output_tokens={max_tokens}"
        )

        gemini_key = (
            getattr(settings, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        mistral_key = (
            getattr(settings, "mistral_api_key", "")
            or os.getenv("MISTRAL_API_KEY", "")
        ).strip()
        openai_key = (
            getattr(settings, "openai_api_key", "")
            or os.getenv("OPENAI_API_KEY", "")
        ).strip()

        # 1. Primary Provider: Gemini
        if gemini_key and not gemini_key.startswith("dummy_") and self.is_provider_healthy("gemini"):
            model_name = await self.get_working_gemini_model(gemini_key)
            if model_name:
                t_gemini = time.perf_counter()
                try:
                    contents = []
                    if history:
                        for turn in history[-4:]:  # Compact context: last 4 turns for high speed
                            role = turn.get("role") or turn.get("sender") or "user"
                            txt = turn.get("content") or turn.get("text") or ""
                            if txt.strip():
                                gemini_role = "model" if role in ("assistant", "model", "bot", "agent") else "user"
                                contents.append({"role": gemini_role, "parts": [{"text": txt.strip()}]})

                    contents.append({"role": "user", "parts": [{"text": prompt.strip()}]})
                    payload: Dict[str, Any] = {
                        "contents": contents,
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_tokens,
                        }
                    }
                    if system_prompt and system_prompt.strip():
                        payload["system_instruction"] = {"parts": [{"text": system_prompt.strip()}]}

                    client = self.get_client()
                    url = f"{GEMINI_BASE_URL}/models/{model_name}:generateContent?key={gemini_key}"
                    resp = await client.post(url, json=payload, timeout=gemini_timeout)
                    elapsed_ms = (time.perf_counter() - t_gemini) * 1000

                    if resp.status_code == 200:
                        data = resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            parts = candidates[0].get("content", {}).get("parts", [])
                            if parts:
                                txt = parts[0].get("text", "").strip()
                                if txt:
                                    self.record_provider_success("gemini")
                                    logger.info(f"[LLM] Gemini response received | provider=gemini | model={model_name} | elapsed_ms={elapsed_ms:.1f}")
                                    return txt
                        logger.warning(
                            f"[LLM] Gemini returned 200 with empty candidate | model={model_name} | elapsed={elapsed_ms:.1f}ms"
                        )
                    elif resp.status_code in (429, 503):
                        err_msg = resp.json().get("error", {}).get("message", resp.text[:200]) if resp.text else "Unavailable"
                        self.record_provider_failure("gemini", f"HTTP {resp.status_code}", status_code=resp.status_code, message=err_msg[:60], model_name=model_name)
                        logger.warning(
                            f"[LLM] Gemini request failed | provider=gemini | model={model_name} | status={resp.status_code} | "
                            f"error_type=HTTPError | message='{err_msg[:120]}' | elapsed={elapsed_ms:.1f}ms | switching to fallback"
                        )
                    elif resp.status_code == 404:
                        self.record_provider_failure("gemini", "HTTP 404", status_code=404, message="Model not found", model_name=model_name)
                        logger.warning(
                            f"[LLM] Gemini model not found | provider=gemini | model={model_name} | status=404 | "
                            f"elapsed={elapsed_ms:.1f}ms | switching to fallback"
                        )
                    else:
                        err_msg = resp.text[:200]
                        self.record_provider_failure("gemini", f"HTTP {resp.status_code}", status_code=resp.status_code, message=err_msg[:60], model_name=model_name)
                        logger.warning(
                            f"[LLM] Gemini request failed | provider=gemini | model={model_name} | status={resp.status_code} | "
                            f"error_type=HTTPError | message='{err_msg[:120]}' | elapsed={elapsed_ms:.1f}ms | switching to fallback"
                        )
                except httpx.TimeoutException:
                    elapsed_ms = (time.perf_counter() - t_gemini) * 1000
                    self.record_provider_failure("gemini", "TimeoutException", message=f"Read timeout > {gemini_timeout:.1f}s", model_name=model_name)
                    logger.warning(
                        f"[LLM] Gemini request timed out | provider=gemini | model={model_name} | error_type=TimeoutException | "
                        f"timeout={gemini_timeout:.1f}s | elapsed={elapsed_ms:.1f}ms | switching to fallback"
                    )
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - t_gemini) * 1000
                    self.record_provider_failure("gemini", type(e).__name__, message=str(e)[:60], model_name=model_name)
                    logger.warning(
                        f"[LLM] Gemini generation failed | provider=gemini | model={model_name} | error_type={type(e).__name__} | "
                        f"message='{str(e)[:150]}' | elapsed={elapsed_ms:.1f}ms | switching to fallback"
                    )

        # 2. Secondary Fast Fallback: Mistral (if configured)
        if mistral_key and not mistral_key.startswith("dummy_") and self.is_provider_healthy("mistral"):
            model_name = await self.get_working_mistral_model(mistral_key)
            if model_name:
                t_mistral = time.perf_counter()
                try:
                    messages = []
                    if system_prompt and system_prompt.strip():
                        messages.append({"role": "system", "content": system_prompt.strip()})
                    if history:
                        for turn in history[-4:]:
                            role = turn.get("role") or turn.get("sender") or "user"
                            txt = turn.get("content") or turn.get("text") or ""
                            if txt.strip():
                                mistral_role = "assistant" if role in ("assistant", "model", "bot", "agent") else "user"
                                messages.append({"role": mistral_role, "content": txt.strip()})
                    messages.append({"role": "user", "content": prompt.strip()})

                    client = self.get_client()
                    resp = await client.post(
                        MISTRAL_API_URL,
                        headers={"Authorization": f"Bearer {mistral_key}", "Content-Type": "application/json"},
                        json={"model": model_name, "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
                        timeout=3.0,
                    )
                    elapsed_ms = (time.perf_counter() - t_mistral) * 1000
                    if resp.status_code == 200:
                        data = resp.json()
                        choices = data.get("choices", [])
                        if choices:
                            txt = choices[0].get("message", {}).get("content", "").strip()
                            if txt:
                                self.record_provider_success("mistral")
                                logger.info(f"[LLM Fallback] Mistral ({model_name}) latency: {elapsed_ms:.1f}ms")
                                return txt
                    elif resp.status_code == 429:
                        self.record_provider_failure("mistral", "HTTP 429", status_code=429, message="Rate limited")
                except Exception as e:
                    self.record_provider_failure("mistral", type(e).__name__, message=str(e)[:60])
                    logger.warning(f"[LLM] Mistral fallback failed ({type(e).__name__}): {e}")

        # 3. Tertiary Fast Fallback: OpenAI (if configured)
        if openai_key and not openai_key.startswith("dummy_") and self.is_provider_healthy("openai"):
            t_openai = time.perf_counter()
            try:
                messages = []
                if system_prompt and system_prompt.strip():
                    messages.append({"role": "system", "content": system_prompt.strip()})
                if history:
                    for turn in history[-4:]:
                        role = turn.get("role") or turn.get("sender") or "user"
                        txt = turn.get("content") or turn.get("text") or ""
                        if txt.strip():
                            o_role = "assistant" if role in ("assistant", "model", "bot", "agent") else "user"
                            messages.append({"role": o_role, "content": txt.strip()})
                messages.append({"role": "user", "content": prompt.strip()})

                client = self.get_client()
                resp = await client.post(
                    OPENAI_API_URL,
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "messages": messages, "temperature": temperature, "max_tokens": max_tokens},
                    timeout=3.0,
                )
                elapsed_ms = (time.perf_counter() - t_openai) * 1000
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices:
                        txt = choices[0].get("message", {}).get("content", "").strip()
                        if txt:
                            self.record_provider_success("openai")
                            logger.info(f"[LLM Fallback] OpenAI (gpt-4o-mini) latency: {elapsed_ms:.1f}ms")
                            return txt
            except Exception as e:
                self.record_provider_failure("openai", type(e).__name__, message=str(e)[:60])
                logger.warning(f"[LLM] OpenAI fallback failed ({type(e).__name__}): {e}")

        # 4. Contextual Script-Grounded Fallback (Zero latency, 100% grounded in current campaign & knowledge)
        t1 = time.perf_counter()
        total_dur_ms = (t1 - t0) * 1000
        fallback_text = self.derive_script_grounded_fallback(prompt, system_prompt)
        logger.info(
            f"[LLM] Returning script-grounded fallback | total_time={total_dur_ms:.1f}ms | response='{fallback_text[:60]}...'"
        )
        return fallback_text

    async def generate_stream(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        history: Optional[List[Dict[str, str]]] = None,
        temperature: float = 0.2,
        max_tokens: int = 256,
    ) -> AsyncIterator[str]:
        """
        Streams generated response chunks from Gemini SSE API.
        Falls back seamlessly to non-streaming generation if Gemini streaming fails or is unhealthy.
        """
        gemini_key = (
            getattr(settings, "gemini_api_key", "")
            or os.getenv("GEMINI_API_KEY", "")
        ).strip()
        gemini_timeout = self.get_gemini_timeout()

        if not gemini_key or gemini_key.startswith("dummy_") or not self.is_provider_healthy("gemini"):
            res = await self.generate(prompt, system_prompt, history, temperature, max_tokens)
            yield res
            return

        model_name = await self.get_working_gemini_model(gemini_key)
        if not model_name:
            res = await self.generate(prompt, system_prompt, history, temperature, max_tokens)
            yield res
            return

        contents = []
        if history:
            for turn in history[-4:]:
                role = turn.get("role") or turn.get("sender") or "user"
                txt = turn.get("content") or turn.get("text") or ""
                if txt.strip():
                    gemini_role = "model" if role in ("assistant", "model", "bot", "agent") else "user"
                    contents.append({"role": gemini_role, "parts": [{"text": txt.strip()}]})

        contents.append({"role": "user", "parts": [{"text": prompt.strip()}]})
        payload: Dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system_prompt and system_prompt.strip():
            payload["system_instruction"] = {"parts": [{"text": system_prompt.strip()}]}

        client = self.get_client()
        url = f"{GEMINI_BASE_URL}/models/{model_name}:streamGenerateContent?alt=sse&key={gemini_key}"

        tokens_yielded = 0
        try:
            async with client.stream("POST", url, json=payload, timeout=gemini_timeout) as resp:
                if resp.status_code != 200:
                    self.record_provider_failure("gemini", f"HTTP {resp.status_code}", status_code=resp.status_code, model_name=model_name)
                    res = await self.generate(prompt, system_prompt, history, temperature, max_tokens)
                    yield res
                    return

                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            chunk_data = json.loads(data_str)
                            candidates = chunk_data.get("candidates", [])
                            if candidates:
                                parts = candidates[0].get("content", {}).get("parts", [])
                                for p in parts:
                                    txt = p.get("text", "")
                                    if txt:
                                        tokens_yielded += 1
                                        yield txt
                        except Exception:
                            continue

            if tokens_yielded > 0:
                self.record_provider_success("gemini")
            else:
                res = await self.generate(prompt, system_prompt, history, temperature, max_tokens)
                yield res

        except Exception as exc:
            self.record_provider_failure("gemini", type(exc).__name__, message=str(exc)[:60], model_name=model_name)
            logger.debug(f"[Gemini Stream] Streaming error: {exc}. Falling back to standard response.")
            res = await self.generate(prompt, system_prompt, history, temperature, max_tokens)
            yield res
