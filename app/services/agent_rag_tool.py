from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.services.rag_service import RAGService


async def search_knowledge(
    db: AsyncSession,
    enterprise_id: str,
    rag_id: str,
    query: str,
    top_k: int = 5
) -> List[Dict[str, Any]]:
    """
    RAG Knowledge Search tool for AI Agents & Voice Agents.
    Enforces enterprise_id and rag_id metadata filtering to prevent cross-enterprise leaks.
    """
    if not enterprise_id or not rag_id or not query:
        logger.warning("search_knowledge called with missing enterprise_id, rag_id, or query")
        return []

    try:
        results = await RAGService.search_chunks(
            db=db,
            query=query,
            enterprise_id=enterprise_id,
            rag_id=rag_id,
            top_k=top_k
        )
        return results
    except Exception as e:
        logger.error(f"AI Agent search_knowledge error for enterprise={enterprise_id}, rag={rag_id}: {e}")
        return []
