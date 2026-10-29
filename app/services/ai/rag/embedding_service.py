"""
Embedding Service for RAG Vector Search.
Generates semantic embeddings using Hugging Face Sentence Transformers (multilingual-MiniLM-L12-v2).
Provides caching and deterministic normalized vectors.
"""
from __future__ import annotations

import hashlib
from typing import Dict, List
from loguru import logger

from app.rag.embedding_service import (
    generate_text_embedding as _generate_sentence_transformer_embedding,
    generate_fallback_embedding,
)

_embedding_cache: Dict[str, List[float]] = {}


def generate_mock_dense_embedding(text: str, dim: int = 384) -> List[float]:
    """Generates a deterministic normalized dense vector for test / offline fallback."""
    return generate_fallback_embedding(text, dim=dim)


def generate_text_embedding(text: str) -> List[float]:
    """
    Generates a 384-dimensional normalized embedding vector for the given text
    using Hugging Face Sentence Transformers with in-memory caching.
    """
    if not text or not text.strip():
        return generate_fallback_embedding("", 384)

    cleaned = text.replace("\n", " ").strip()
    cache_key = hashlib.md5(cleaned.encode("utf-8")).hexdigest()
    if cache_key in _embedding_cache:
        return _embedding_cache[cache_key]

    raw_vec = _generate_sentence_transformer_embedding(cleaned)
    import numpy as np
    arr = np.array(raw_vec, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    vec = arr.tolist()
    _embedding_cache[cache_key] = vec
    return vec


class EmbeddingService:
    """OOP interface for embedding generation and vector utilities."""

    @classmethod
    def generate_embedding(cls, text: str) -> List[float]:
        return generate_text_embedding(text)

    @classmethod
    def generate_batch_embeddings(cls, texts: List[str]) -> List[List[float]]:
        return [generate_text_embedding(t) for t in texts]

    @classmethod
    def cosine_similarity(cls, vec_a: List[float], vec_b: List[float]) -> float:
        import numpy as np
        a = np.array(vec_a, dtype=np.float32)
        b = np.array(vec_b, dtype=np.float32)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))


