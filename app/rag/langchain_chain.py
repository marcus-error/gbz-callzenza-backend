"""
LangChain RAG Chain execution.
Formats context, builds prompt, and invokes Gemini or Mistral via the unified LLM layer.
"""
from __future__ import annotations

import os
import re
from typing import List, Dict, Any, Optional
from loguru import logger

try:
    from langchain_core.prompts import PromptTemplate
except Exception:
    class PromptTemplate:
        def __init__(self, input_variables: List[str], template: str):
            self.input_variables = input_variables
            self.template = template

        def format(self, **kwargs) -> str:
            res = self.template
            for k, v in kwargs.items():
                res = res.replace(f"{{{k}}}", str(v))
            return res

try:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
except Exception:
    class RecursiveCharacterTextSplitter:
        def __init__(self, chunk_size: int = 500, chunk_overlap: int = 50, separators: Optional[List[str]] = None):
            self.chunk_size = chunk_size
            self.chunk_overlap = chunk_overlap

        def split_text(self, text: str) -> List[str]:
            if not text:
                return []
            chunks = []
            start = 0
            while start < len(text):
                end = min(start + self.chunk_size, len(text))
                chunks.append(text[start:end])
                if end == len(text):
                    break
                start = max(end - self.chunk_overlap, start + 1)
            return chunks

from app.core.config import get_settings
from app.services.ai.llm_service import get_llm_service
from app.services.ai.prompt_service import PromptService

settings = get_settings()

RAG_PROMPT_TEMPLATE = """
You are the CallZenza Enterprise AI Assistant.

SYSTEM INSTRUCTIONS & GROUNDING RULES:
1. Grounding: You MUST answer the user's question using ONLY the provided retrieved context snippets from the enterprise knowledge base.
2. No Hallucinations: Do NOT invent or make up company-specific facts, policies, products, or procedures that are not supported by the context.
3. Fallback: If the required information is not available in the retrieved context, clearly state: "I couldn't find that information in the available company knowledge base."
4. Multilingual & Tanglish: Answer in the same language and style as the user's question (English, Tamil, Tanglish, Hindi, Hinglish, or mixed-language).
5. Clean Formatting: Format your response using clean, well-structured markdown without internal metadata headers.

RETRIEVED KNOWLEDGE CONTEXT:
{context}

CONVERSATION HISTORY:
{history}

USER QUESTION:
{question}

ANSWER:
"""

prompt_template = PromptTemplate(
    input_variables=["context", "history", "question"],
    template=RAG_PROMPT_TEMPLATE
)


def split_document_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
    """
    Splits text into logical section chunks for TXT scripts / structured text,
    falling back to LangChain RecursiveCharacterTextSplitter for general documents.
    """
    if not text or not text.strip():
        return []

    # Check for logical script section headers (e.g. [PRODUCT INFORMATION], Pricing:, Objections:, Greeting:, Closing:)
    section_pattern = r"(?=(?:^|\n\n)(?:\[?[A-Z0-9\s\_]{3,}:?\]?|1\.\s*Greeting|2\.\s*Product|3\.\s*Pricing|4\.\s*Objection|5\.\s*Closing|Product Information|Pricing|Objections?|Qualification|Handoff|Closing)\b)"
    sections = [s.strip() for s in re.split(section_pattern, text, flags=re.IGNORECASE) if s and s.strip()]

    chunks: List[str] = []
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", " ", ""]
    )

    if len(sections) > 1:
        for sec in sections:
            if len(sec) <= chunk_size * 1.5:
                chunks.append(sec)
            else:
                chunks.extend(splitter.split_text(sec))
    else:
        chunks = splitter.split_text(text)

    return [c for c in chunks if c and c.strip()]



async def run_langchain_rag_chain(
    question: str,
    retrieved_chunks: List[Dict[str, Any]],
    history_str: str = "",
    llm_model: Optional[str] = None,
    llm_provider: Optional[str] = None,
    llm_api_base: Optional[str] = None
) -> str:
    """
    Orchestrates the LangChain RAG pipeline with Gemini / Mistral LLM layer.
    """
    context_blocks = []
    for idx, chunk in enumerate(retrieved_chunks):
        doc_name = chunk.get("document_name", "Document")
        txt = chunk.get("text", "")
        context_blocks.append(f"[{doc_name} - Chunk {idx+1}]:\n{txt}")

    context_str = "\n\n".join(context_blocks) if context_blocks else "No relevant document chunks found."
    
    formatted_prompt = prompt_template.format(
        context=context_str,
        history=history_str or "No previous history.",
        question=question
    )

    llm = get_llm_service(llm_provider)
    try:
        raw_ans = await llm.generate_response(
            prompt=question,
            system_prompt=RAG_PROMPT_TEMPLATE.replace("{context}", context_str).replace("{history}", history_str or "None"),
        )
        return PromptService.clean_response(raw_ans)
    except Exception as exc:
        logger.error(f"[LangChain RAG] LLM call failed: {exc}")
        return "I couldn't find that information in the available company knowledge base."
