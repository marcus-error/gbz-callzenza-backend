"""
Gemini Service Bridge.
Provides embedding generation, answer generation, and integration with the unified AI service layer.
"""
from __future__ import annotations

import os
import time
import math
import json
import httpx
import re
from typing import List, Optional, Dict, Any
from loguru import logger
from dotenv import load_dotenv

from app.services.ai.rag.embedding_service import generate_text_embedding, generate_mock_dense_embedding
from app.services.ai.llm_service import get_llm_service
from app.services.ai.prompt_service import PromptService

load_dotenv()

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


def generate_mock_embedding(text: str, dim: int = 768) -> List[float]:
    """Generates a normalized deterministic dense embedding vector for testing/fallback."""
    return generate_mock_dense_embedding(text, dim)


def generate_embedding(text: str) -> List[float]:
    """Generates a semantic embedding vector for a text string."""
    return generate_text_embedding(text)


STOP_WORDS = {"who", "is", "the", "of", "a", "an", "what", "where", "how", "tell", "me", "about", "can", "you", "please", "in", "on", "at", "to", "for", "with", "this", "that", "does", "do", "it", "work", "works"}


def clean_answer_text(text: str, query: str = "") -> str:
    """Extracts and returns only the clean, direct natural language answer text without prefixes or headers."""
    return PromptService.clean_response(text)


def generate_offline_general_answer(
    query: str, 
    retrieved_chunks: Optional[List[str]] = None,
    history: Optional[List[Dict[str, str]]] = None
) -> str:
    """System error fallback when LLM API connection is offline or unavailable."""
    return "I am currently unable to generate a response. Please try again in a moment."


async def generate_answer(
    query: str,
    retrieved_chunks: List[str],
    system_prompt: Optional[str] = None,
    model_name: Optional[str] = None,
    temperature: float = 0.2,
    fallback_message: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    db=None,
    business_id: Optional[str] = None,
    history: Optional[List[Dict[str, str]]] = None
) -> str:
    """
    Generates a grounded conversational response using the unified LLM Service layer (Gemini / Mistral).
    """
    if not query or not query.strip():
        return "Please provide a question."

    context_str = "\n\n---\n\n".join(retrieved_chunks) if retrieved_chunks else ""

    sys_instruction = PromptService.build_system_prompt(
        agent_name="CallZenza AI Assistant",
        agent_persona=system_prompt,
        retrieved_context=context_str,
        has_relevant_context=bool(retrieved_chunks),
    )

    llm_service = get_llm_service()
    try:
        raw_ans = await llm_service.generate_response(
            prompt=query.strip(),
            system_prompt=sys_instruction,
            history=history,
            temperature=temperature,
        )
        return clean_answer_text(raw_ans, query)
    except Exception as e:
        logger.error(f"[generate_answer] LLM call failed: {e}")
        return fallback_message or "I don't have enough information available to answer that question."