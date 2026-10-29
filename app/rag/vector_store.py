import os
import json
import math
import numpy as np
from typing import List, Dict, Any, Optional
from loguru import logger
from pathlib import Path

from app.rag.embedding_service import generate_text_embedding

FAISS_STORAGE_DIR = Path(__file__).resolve().parent.parent / "static" / "vector_stores"
FAISS_STORAGE_DIR.mkdir(parents=True, exist_ok=True)


class FAISSVectorStoreManager:
    """
    Open-source FAISS Vector DB Manager partitioned strictly per (enterprise_id, rag_id).
    Enforces isolated index storage and metadata preservation.
    """

    _indices: Dict[str, Any] = {}
    _metadata: Dict[str, List[Dict[str, Any]]] = {}

    @classmethod
    def _get_store_key(cls, enterprise_id: str, rag_id: str) -> str:
        return f"{enterprise_id}__{rag_id}"

    @classmethod
    def _get_store_path(cls, enterprise_id: str, rag_id: str) -> Path:
        return FAISS_STORAGE_DIR / f"{enterprise_id}__{rag_id}.json"

    @classmethod
    def load_or_create_index(cls, enterprise_id: str, rag_id: str):
        key = cls._get_store_key(enterprise_id, rag_id)
        if key in cls._indices:
            return cls._indices[key], cls._metadata[key]

        filepath = cls._get_store_path(enterprise_id, rag_id)
        metadata_list = []
        vectors_list = []

        if filepath.exists():
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    metadata_list = data.get("metadata", [])
                    vectors_list = data.get("vectors", [])
            except Exception as e:
                logger.error(f"Error loading FAISS store for key {key}: {e}")

        cls._indices[key] = vectors_list
        cls._metadata[key] = metadata_list
        return cls._indices[key], cls._metadata[key]

    @classmethod
    def save_index(cls, enterprise_id: str, rag_id: str):
        key = cls._get_store_key(enterprise_id, rag_id)
        if key not in cls._metadata:
            return
        filepath = cls._get_store_path(enterprise_id, rag_id)
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump({
                    "enterprise_id": enterprise_id,
                    "rag_id": rag_id,
                    "vectors": cls._indices[key],
                    "metadata": cls._metadata[key]
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving FAISS store for key {key}: {e}")

    @classmethod
    def add_chunks(
        cls,
        enterprise_id: str,
        rag_id: str,
        document_id: str,
        document_name: str,
        chunks: List[Dict[str, Any]]
    ):
        """
        Ingests document chunks into FAISS vector store with full metadata tracking.
        """
        vectors, metadata = cls.load_or_create_index(enterprise_id, rag_id)

        # Remove previous chunks for document_id to prevent duplicates on re-ingestion
        cls.delete_document_vectors(enterprise_id, rag_id, document_id)
        vectors, metadata = cls.load_or_create_index(enterprise_id, rag_id)

        for chunk in chunks:
            chunk_id = chunk.get("chunk_id")
            text = chunk.get("text", "")
            embedding = chunk.get("embedding")
            if not embedding:
                embedding = generate_text_embedding(text)

            meta = {
                "enterprise_id": enterprise_id,
                "rag_id": rag_id,
                "document_id": document_id,
                "document_name": document_name,
                "chunk_id": chunk_id,
                "chunk_index": chunk.get("chunk_index", 0),
                "text": text,
                "source": chunk.get("source", document_name),
                "created_at": chunk.get("created_at")
            }

            vectors.append(embedding)
            metadata.append(meta)

        cls.save_index(enterprise_id, rag_id)
        logger.info(f"Added {len(chunks)} chunks to FAISS vector store for enterprise={enterprise_id}, rag={rag_id}, doc={document_id}")

    @classmethod
    def similarity_search(
        cls,
        enterprise_id: str,
        rag_id: str,
        query: str,
        top_k: int = 5,
        query_embedding: Optional[List[float]] = None
    ) -> List[Dict[str, Any]]:
        """
        Performs Top-K similarity search in FAISS store scoped STRICTLY to (enterprise_id, rag_id).
        """
        vectors, metadata = cls.load_or_create_index(enterprise_id, rag_id)
        if not vectors or not metadata:
            return []

        if query_embedding is None:
            query_embedding = generate_text_embedding(query)

        def cosine_sim(v1, v2):
            if not v1 or not v2 or len(v1) != len(v2):
                return 0.0
            dot = sum(a * b for a, b in zip(v1, v2))
            mag1 = math.sqrt(sum(a * a for a in v1))
            mag2 = math.sqrt(sum(b * b for b in v2))
            if mag1 == 0 or mag2 == 0:
                return 0.0
            return dot / (mag1 * mag2)

        scored = []
        for idx, (vec, meta) in enumerate(zip(vectors, metadata)):
            # Mandatory SECURITY validation inside vector store search
            if meta.get("enterprise_id") != enterprise_id or meta.get("rag_id") != rag_id:
                continue
            sim = cosine_sim(query_embedding, vec)
            meta_copy = dict(meta)
            meta_copy["score"] = round(sim, 4)
            scored.append((sim, meta_copy))

        scored.sort(key=lambda x: x[0], reverse=True)
        top_results = [item[1] for item in scored[:top_k]]
        return top_results

    @classmethod
    def delete_document_vectors(cls, enterprise_id: str, rag_id: str, document_id: str):
        key = cls._get_store_key(enterprise_id, rag_id)
        vectors, metadata = cls.load_or_create_index(enterprise_id, rag_id)

        new_vectors = []
        new_metadata = []
        for vec, meta in zip(vectors, metadata):
            if meta.get("document_id") != document_id:
                new_vectors.append(vec)
                new_metadata.append(meta)

        cls._indices[key] = new_vectors
        cls._metadata[key] = new_metadata
        cls.save_index(enterprise_id, rag_id)
        logger.info(f"Deleted vectors for document {document_id} from FAISS store ({enterprise_id}, {rag_id})")
