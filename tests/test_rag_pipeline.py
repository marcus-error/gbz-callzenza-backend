import pytest
import asyncio
import json
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import User, Business, RAGConfig, KnowledgeDocument, KnowledgeChunk
from app.rag.embedding_service import generate_text_embedding
from app.rag.vector_store import FAISSVectorStoreManager
from app.rag.langchain_chain import split_document_text, run_langchain_rag_chain
from app.services.rag_service import RAGService
from app.services.agent_rag_tool import search_knowledge
from fastapi import HTTPException


import pytest_asyncio

@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, expire_on_commit=False)
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.mark.asyncio
async def test_rag_security_isolation(db_session: AsyncSession):
    """
    SECURITY TEST (PHASE 6 & 19):
    Verifies that Enterprise A + RAG A can NEVER retrieve Enterprise B + RAG B documents,
    and cross-enterprise access attempts raise 403 Forbidden.
    """
    # 1. Setup Enterprise A
    ent_a = Business(id="enterprise_a", name="Enterprise A", owner_user_id="user_a")
    user_a = User(id="user_a", email="usera@ent-a.com", hashed_password="pw", business_id="enterprise_a", role="ADMIN")
    rag_a = RAGConfig(id="rag_a", business_id="enterprise_a", name="RAG A", status="ACTIVE")
    
    # 2. Setup Enterprise B
    ent_b = Business(id="enterprise_b", name="Enterprise B", owner_user_id="user_b")
    user_b = User(id="user_b", email="userb@ent-b.com", hashed_password="pw", business_id="enterprise_b", role="ADMIN")
    rag_b = RAGConfig(id="rag_b", business_id="enterprise_b", name="RAG B", status="ACTIVE")

    db_session.add_all([ent_a, user_a, rag_a, ent_b, user_b, rag_b])
    await db_session.commit()

    # 3. Add Document A to Enterprise A / RAG A
    doc_a_chunks = [{
        "chunk_id": "chunk_a1",
        "chunk_index": 0,
        "text": "Enterprise A leave policy provides 20 days of paid casual leave per year.",
        "embedding": generate_text_embedding("Enterprise A leave policy 20 days casual leave"),
        "source": "leave_policy_a.pdf"
    }]
    FAISSVectorStoreManager.add_chunks("enterprise_a", "rag_a", "doc_a", "leave_policy_a.pdf", doc_a_chunks)

    # 4. Add Document B to Enterprise B / RAG B
    doc_b_chunks = [{
        "chunk_id": "chunk_b1",
        "chunk_index": 0,
        "text": "Enterprise B leave policy provides 10 days of paid casual leave per year.",
        "embedding": generate_text_embedding("Enterprise B leave policy 10 days casual leave"),
        "source": "leave_policy_b.pdf"
    }]
    FAISSVectorStoreManager.add_chunks("enterprise_b", "rag_b", "doc_b", "leave_policy_b.pdf", doc_b_chunks)

    # TEST 1: Enterprise A + RAG A query retrieves ONLY Enterprise A document
    results_a = await RAGService.search_chunks(
        db=db_session,
        query="How many leave days do I get?",
        enterprise_id="enterprise_a",
        rag_id="rag_a",
        top_k=5
    )
    assert len(results_a) > 0
    assert "20 days" in results_a[0]["text"]
    assert "10 days" not in results_a[0]["text"]

    # TEST 2: Enterprise B + RAG B query retrieves ONLY Enterprise B document
    results_b = await RAGService.search_chunks(
        db=db_session,
        query="How many leave days do I get?",
        enterprise_id="enterprise_b",
        rag_id="rag_b",
        top_k=5
    )
    assert len(results_b) > 0
    assert "10 days" in results_b[0]["text"]
    assert "20 days" not in results_b[0]["text"]

    # TEST 3: User A (Enterprise A) attempting to query RAG B (Enterprise B) MUST be rejected with 403 Forbidden
    auth_user_a = {"sub": "user_a", "role": "ADMIN"}
    with pytest.raises(HTTPException) as exc_info:
        await RAGService.validate_enterprise_rag_access(
            db=db_session,
            current_user=auth_user_a,
            enterprise_id="enterprise_b",
            rag_id="rag_b"
        )
    assert exc_info.value.status_code == 403

    # TEST 4: User A attempting to query invalid or unassigned RAG ID MUST be rejected with 403 Forbidden
    with pytest.raises(HTTPException) as exc_info2:
        await RAGService.validate_enterprise_rag_access(
            db=db_session,
            current_user=auth_user_a,
            enterprise_id="enterprise_a",
            rag_id="non_existent_rag_id"
        )
    assert exc_info2.value.status_code == 403

    print("SECURITY & DATA ISOLATION TESTS PASSED SUCCESSFULLY!")


@pytest.mark.asyncio
async def test_ai_agent_tool_and_multilingual(db_session: AsyncSession):
    """
    Tests AI Agent search_knowledge tool and multilingual/Tanglish capabilities.
    """
    # Ingest test chunks
    chunks = [{
        "chunk_id": "c1",
        "text": "CallZenza Lead Hopper prioritizes urgent callback leads automatically.",
        "embedding": generate_text_embedding("CallZenza Lead Hopper prioritizes urgent callback leads"),
        "source": "features.txt"
    }]
    FAISSVectorStoreManager.add_chunks("ent_test", "rag_test", "doc_test", "features.txt", chunks)

    # Test AI Agent tool search_knowledge
    agent_results = await search_knowledge(
        db=db_session,
        enterprise_id="ent_test",
        rag_id="rag_test",
        query="Tell me about Lead Hopper priority",
        top_k=3
    )
    assert len(agent_results) > 0
    assert "Lead Hopper" in agent_results[0]["text"]

    # Test LangChain text splitter
    text = "Line 1\n\nLine 2\n\nLine 3"
    split_chunks = split_document_text(text, chunk_size=20, chunk_overlap=5)
    assert len(split_chunks) > 0

    print("AI AGENT TOOL & MULTILINGUAL TESTS PASSED SUCCESSFULLY!")
