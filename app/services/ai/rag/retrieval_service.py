"""
Agent-Specific RAG Retrieval Service.
Enforces strict agent_id and organization_id isolation — only active documents and chunks
belonging to the specified Agent & Business are searched.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple
from sqlalchemy import select, or_, and_
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.core.config import get_settings
from app.db.models import KnowledgeLibrary, KnowledgeDocument, KnowledgeChunk
from app.services.ai.rag.embedding_service import generate_text_embedding

settings = get_settings()

NON_RAG_PATTERNS = [
    r'^(?:hi|hello|hey|good\s+(?:morning|afternoon|evening|day)|greetings)(?:$|\s|\.|\!|\?)',
    r'^(?:ok|okay|sure|yeah|yes|yep|nope|no|alright|fine|thanks|thank\s+you|bye|goodbye|see\s+you)(?:$|\s|\.|\!|\?)',
    r'^(?:how\s+are\s+you|who\s+are\s+you|what\s+is\s+your\s+name)(?:$|\s|\.|\!|\?)',
    r'^(?:can\s+you\s+hear\s+me|are\s+you\s+there|test|hello\s*\?)(?:$|\s|\.|\!|\?)',
    r'^(?:talk\s+in|speak\s+in|switch\s+to|change\s+language|tamil|english|hindi|telugu)(?:$|\s|\.|\!|\?)',
    r'^(?:வணக்கம்|நன்றி|நன்றி\s+அய்யா|சரி|ஆமாம்|இல்லை|எப்படி\s+இருக்கீங்க)(?:$|\s|\.|\!|\?)',
]


def is_business_knowledge_required(query: str) -> bool:
    """
    Lightweight heuristic classifier: Determines if the caller's message requires
    business knowledge retrieval from the Agent Script.
    Returns False for greetings, confirmations, language switches, and general chit-chat.
    """
    if not query or not query.strip():
        return False

    clean = query.strip().lower()

    # If query matches pure conversational / non-RAG patterns
    for pat in NON_RAG_PATTERNS:
        if re.search(pat, clean):
            # If it's a short greeting/confirmation (<= 5 words), skip RAG
            if len(clean.split()) <= 5:
                return False

    return True


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    """Computes cosine similarity between two numeric vectors."""
    if not v1 or not v2 or len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    norm_a = math.sqrt(sum(a * a for a in v1))
    norm_b = math.sqrt(sum(b * b for b in v2))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class AgentRAGRetrievalService:
    """Retrieves grounded knowledge strictly partitioned by agent_id, business_id, and active version."""

    @staticmethod
    async def retrieve_agent_knowledge(
        db: AsyncSession,
        agent_id: str,
        query: str,
        business_id: Optional[str] = None,
        top_k: int = 4,
        threshold: float = 0.22,
        force_rag: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Retrieves top relevant knowledge chunks strictly belonging to the active agent & organization.

        :param db: Active SQLAlchemy AsyncSession.
        :param agent_id: Primary agent isolation key.
        :param query: Customer question.
        :param business_id: Optional organization/tenant isolation key.
        :param top_k: Maximum number of chunks to return (compact context).
        :param threshold: Minimum similarity score threshold.
        :param force_rag: If True, bypasses non-RAG classifier.
        :return: List of retrieved chunks with metadata and score.
        """
        if not db or not agent_id or not query or not query.strip():
            return []

        clean_query = query.strip()

        # Step 1: Check if RAG is actually required
        if not force_rag and not is_business_knowledge_required(clean_query):
            logger.debug(f"[RAG] Skipped RAG for non-business query: '{clean_query[:40]}'")
            return []

        query_vec = generate_text_embedding(clean_query)

        # Step 2: Fetch entries from KnowledgeLibrary strictly belonging to this agent_id and active version
        stmt_lib = select(KnowledgeLibrary).where(
            KnowledgeLibrary.agent_id == agent_id,
            KnowledgeLibrary.is_active == True,
            KnowledgeLibrary.source_data != "Uploaded Emotion Guidelines",
            KnowledgeLibrary.source_data != "Dialogue State Tracking"
        )
        if business_id:
            stmt_lib = stmt_lib.where(KnowledgeLibrary.business_id == business_id)

        res_lib = await db.scalars(stmt_lib)
        library_entries = list(res_lib.all())

        # Step 3: Fetch active chunks from KnowledgeDocument / KnowledgeChunk if attached to agent
        stmt_chunks = select(KnowledgeChunk).join(
            KnowledgeDocument, KnowledgeChunk.document_id == KnowledgeDocument.id
        ).where(
            KnowledgeChunk.is_active == True,
            or_(
                KnowledgeChunk.agent_id == agent_id,
                KnowledgeDocument.agent_id == agent_id,
                KnowledgeDocument.agent_key == agent_id,
                KnowledgeChunk.agent_key == agent_id,
            )
        )
        if business_id:
            stmt_chunks = stmt_chunks.where(KnowledgeDocument.business_id == business_id)

        try:
            res_chunks = await db.scalars(stmt_chunks)
            doc_chunks = list(res_chunks.all())
        except Exception:
            doc_chunks = []

        scored_results: List[Tuple[float, Dict[str, Any]]] = []

        # Score KnowledgeLibrary entries
        for entry in library_entries:
            score = 0.0
            if entry.embedding:
                try:
                    emb_list = json.loads(entry.embedding)
                    score = cosine_similarity(query_vec, emb_list)
                except Exception:
                    score = 0.0

            if score == 0.0:
                # Keyword overlap fallback
                q_words = set(re.findall(r'\w+', clean_query.lower()))
                t_words = set(re.findall(r'\w+', (entry.question + " " + entry.answer).lower()))
                if q_words and t_words:
                    overlap = len(q_words.intersection(t_words)) / len(q_words)
                    score = min(0.95, overlap * 0.8)

            if score >= threshold:
                scored_results.append((score, {
                    "source": entry.source_data or "Agent Script",
                    "text": entry.answer,
                    "question": entry.question,
                    "score": round(score, 4),
                    "id": entry.id,
                }))

        # Score KnowledgeChunk entries
        for chunk in doc_chunks:
            score = 0.0
            if chunk.embedding:
                try:
                    emb_list = json.loads(chunk.embedding)
                    score = cosine_similarity(query_vec, emb_list)
                except Exception:
                    score = 0.0
            if score == 0.0:
                q_words = set(re.findall(r'\w+', clean_query.lower()))
                t_words = set(re.findall(r'\w+', chunk.text.lower()))
                if q_words and t_words:
                    overlap = len(q_words.intersection(t_words)) / len(q_words)
                    score = min(0.95, overlap * 0.8)

            if score >= threshold:
                meta = chunk.meta_info or {}
                scored_results.append((score, {
                    "source": meta.get("document_name", "Document Chunk"),
                    "text": chunk.text,
                    "question": "",
                    "score": round(score, 4),
                    "id": chunk.id,
                }))

        # Sort descending by similarity score
        scored_results.sort(key=lambda x: x[0], reverse=True)

        top_chunks = [item[1] for item in scored_results[:top_k]]
        logger.info(f"[RAG] Retrieved {len(top_chunks)} chunks for agent_id='{agent_id}' (Query: '{clean_query[:50]}')")
        return top_chunks

