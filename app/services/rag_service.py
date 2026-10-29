import json
import math
import time
from typing import List, Dict, Any, Optional, Tuple
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, func
from loguru import logger

from app.db.models import (
    KnowledgeChunk, 
    KnowledgeDocument, 
    ChatbotConfig, 
    RAGConfig, 
    RAGCache, 
    RAGMiss, 
    RAGAgent, 
    User
)
from app.rag.embedding_service import generate_text_embedding
from app.rag.vector_store import FAISSVectorStoreManager
from app.rag.langchain_chain import run_langchain_rag_chain
from app.services.gemini_service import generate_answer, generate_embedding


class RAGService:
    """Production-Ready Enterprise RAG Service with FAISS, LangChain & Security Isolation."""

    @classmethod
    async def get_or_create_default_rag_config(
        cls, 
        db: AsyncSession, 
        business_id: str
    ) -> RAGConfig:
        """Retrieves or initializes the primary RAGConfig for a tenant business/enterprise."""
        stmt = select(RAGConfig).where(
            RAGConfig.business_id == business_id,
            RAGConfig.status == "ACTIVE"
        )
        res = await db.scalars(stmt)
        config = res.first()
        if not config:
            config = RAGConfig(
                business_id=business_id,
                name="Default Enterprise RAG",
                description="Primary Enterprise RAG Configuration",
                status="ACTIVE"
            )
            db.add(config)
            await db.commit()
            await db.refresh(config)
        return config

    @classmethod
    async def get_chatbot_config(
        cls, 
        db: AsyncSession, 
        business_id: str = "default_enterprise"
    ) -> ChatbotConfig:
        """Retrieves or initializes ChatbotConfig for admin chatbot settings."""
        stmt = select(ChatbotConfig).where(ChatbotConfig.business_id == business_id)
        res = await db.scalars(stmt)
        config = res.first()
        if not config:
            config = ChatbotConfig(
                business_id=business_id,
                model_name="gemini-2.0-flash",
                temperature=0.7,
                top_k=5,
                similarity_threshold=0.25
            )
            db.add(config)
            await db.commit()
            await db.refresh(config)
        return config

    @classmethod
    async def get_config(
        cls, 
        db: AsyncSession, 
        business_id: str = "default_enterprise"
    ) -> ChatbotConfig:
        """Alias for get_chatbot_config for chatbot admin compatibility."""
        return await cls.get_chatbot_config(db, business_id)

    @classmethod
    async def answer_question(
        cls,
        db: AsyncSession,
        query: str,
        business_id: Optional[str] = None,
        agent_key: Optional[int] = None,
        is_global: bool = False,
        conversation_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        High-level chatbot entrypoint combining enterprise FAISS vector search, 
        KnowledgeChunk fallback, and Gemini text generation.
        """
        start_time = time.time()
        ent_id = business_id or "default_enterprise"
        config = await cls.get_or_create_default_rag_config(db, ent_id)

        # Retrieve top chunks via FAISS + DB fallback
        retrieved_chunks = await cls.search_chunks(
            db=db,
            query=query,
            enterprise_id=ent_id,
            rag_id=config.id,
            top_k=config.top_k or 5
        )

        # Check agent_key fallback if needed
        if agent_key and not retrieved_chunks:
            stmt = select(KnowledgeChunk).where(KnowledgeChunk.agent_key == agent_key)
            agent_res = await db.scalars(stmt)
            for chunk in agent_res.all():
                meta = chunk.meta_info or {}
                retrieved_chunks.append({
                    "enterprise_id": ent_id,
                    "rag_id": config.id,
                    "chunk_id": chunk.id,
                    "document_id": chunk.document_id,
                    "document_name": meta.get("document_name", "Document"),
                    "text": chunk.text,
                    "score": 0.80,
                    "source": meta.get("filename", "Agent Knowledge")
                })

        chunk_texts = [c.get("text", "") for c in retrieved_chunks if c.get("text")]
        
        answer_text = await generate_answer(
            query=query,
            retrieved_chunks=chunk_texts,
            system_prompt=config.system_prompt or "You are CallZenza AI assistant. Answer using the provided context."
        )

        latency_ms = int((time.time() - start_time) * 1000)

        return {
            "answer": answer_text,
            "retrieved_context": retrieved_chunks,
            "latency_ms": latency_ms,
            "sources": [c.get("document_name", "Document") for c in retrieved_chunks],
            "enterprise_id": ent_id,
            "rag_id": config.id
        }

    @classmethod
    async def validate_enterprise_rag_access(
        cls,
        db: AsyncSession,
        current_user: Optional[Dict[str, Any]],
        enterprise_id: str,
        rag_id: Optional[str] = None
    ) -> RAGConfig:
        """
        SECURITY REQUIREMENT (PHASE 6):
        Validates authenticated user -> enterprise_id -> allowed_rag_id.
        Cross-enterprise or unassigned RAG ID access MUST be rejected.
        """
        if current_user:
            user_role = current_user.get("role", "VIEWER")
            user_sub = current_user.get("sub")
            if user_sub and user_role != "SUPER_ADMIN":
                user_row = await db.get(User, str(user_sub))
                if user_row and user_row.business_id and user_row.business_id != enterprise_id:
                    logger.warning(f"Security Alert: User {user_sub} from enterprise {user_row.business_id} attempted access to enterprise {enterprise_id}")
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Unauthorized enterprise access attempt blocked."
                    )

        if rag_id:
            rag_cfg = await db.get(RAGConfig, rag_id)
            if not rag_cfg or rag_cfg.business_id != enterprise_id:
                logger.warning(f"Security Alert: RAG ID {rag_id} does not belong to enterprise {enterprise_id}")
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Unauthorized RAG Configuration access attempt blocked."
                )
            return rag_cfg
        
        return await cls.get_or_create_default_rag_config(db, enterprise_id)

    @classmethod
    async def search_chunks(
        cls,
        db: AsyncSession,
        query: str,
        enterprise_id: str,
        rag_id: str,
        top_k: int = 5,
        similarity_threshold: float = 0.20
    ) -> List[Dict[str, Any]]:
        """
        Performs Top-K similarity search in FAISS vector store scoped strictly by (enterprise_id, rag_id).
        Falls back to DB KnowledgeChunk query if FAISS store is empty.
        """
        query_vector = generate_text_embedding(query)
        
        # 1. Search in FAISS vector store
        faiss_results = FAISSVectorStoreManager.similarity_search(
            enterprise_id=enterprise_id,
            rag_id=rag_id,
            query=query,
            top_k=top_k,
            query_embedding=query_vector
        )

        if faiss_results:
            return faiss_results

        # 2. Fallback DB vector chunk search scoped to business_id and rag_id
        stmt = select(KnowledgeChunk).where(
            (KnowledgeChunk.business_id == enterprise_id) | (KnowledgeChunk.business_id.is_(None))
        )
        if rag_id:
            stmt = stmt.where(
                (KnowledgeChunk.rag_id == rag_id) | (KnowledgeChunk.rag_id.is_(None))
            )
        
        res = await db.scalars(stmt)
        chunks = res.all()

        def cosine_similarity(v1: List[float], v2: List[float]) -> float:
            if not v1 or not v2 or len(v1) != len(v2):
                return 0.0
            dot_product = sum(a * b for a, b in zip(v1, v2))
            mag1 = math.sqrt(sum(a * a for a in v1))
            mag2 = math.sqrt(sum(b * b for b in v2))
            if mag1 == 0 or mag2 == 0:
                return 0.0
            return dot_product / (mag1 * mag2)

        scored: List[Tuple[float, KnowledgeChunk]] = []
        for chunk in chunks:
            if not chunk.embedding:
                continue
            try:
                emb_list = json.loads(chunk.embedding)
                sim = cosine_similarity(query_vector, emb_list)
                if sim >= similarity_threshold:
                    scored.append((sim, chunk))
            except Exception:
                continue

        scored.sort(key=lambda x: x[0], reverse=True)
        top_matches = scored[:top_k]

        formatted = []
        for score, chunk in top_matches:
            meta = chunk.meta_info or {}
            formatted.append({
                "enterprise_id": enterprise_id,
                "rag_id": rag_id,
                "chunk_id": chunk.id,
                "document_id": chunk.document_id,
                "document_name": meta.get("document_name", meta.get("filename", "Document")),
                "text": chunk.text,
                "score": round(score, 4),
                "source": meta.get("filename", "Database Knowledge"),
                "created_at": chunk.created_at.isoformat() if chunk.created_at else None
            })

        return formatted

    @classmethod
    async def query_rag_pipeline(
        cls,
        db: AsyncSession,
        enterprise_id: str,
        rag_id: str,
        question: str,
        current_user: Optional[Dict[str, Any]] = None,
        history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Complete Phase 7 RAG Retrieval Pipeline:
        Auth -> Enterprise ID Validation -> RAG ID Validation -> Query Embedding -> FAISS Vector Search -> Top-K -> LangChain -> Answer
        """
        start_time = time.time()

        # Step 1: Validate Security Access
        rag_cfg = await cls.validate_enterprise_rag_access(
            db=db,
            current_user=current_user,
            enterprise_id=enterprise_id,
            rag_id=rag_id
        )

        # Step 2: Vector Search in FAISS Store
        top_chunks = await cls.search_chunks(
            db=db,
            query=question,
            enterprise_id=enterprise_id,
            rag_id=rag_id,
            top_k=rag_cfg.top_k or 5
        )

        # Format history string
        history_str = ""
        if history:
            history_str = "\n".join([f"{h.get('sender', 'user').capitalize()}: {h.get('content', '')}" for h in history])

        # Step 3: Execute LangChain RAG Chain with Open-Source LLM
        answer = await run_langchain_rag_chain(
            question=question,
            retrieved_chunks=top_chunks,
            history_str=history_str,
            llm_model=rag_cfg.llm_model,
            llm_provider=rag_cfg.llm_provider,
            llm_api_base=rag_cfg.llm_api_base
        )

        latency_ms = int((time.time() - start_time) * 1000)

        return {
            "answer": answer,
            "enterprise_id": enterprise_id,
            "rag_id": rag_id,
            "sources": [c.get("document_name", "Document") for c in top_chunks],
            "retrieved_context": top_chunks,
            "latency_ms": latency_ms,
            "has_context": len(top_chunks) > 0
        }
