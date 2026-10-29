"""
Comprehensive Test Suite for Universal Dynamic AI Agent Pipeline.
Tests:
1. SentenceTransformers 384-dim normalized embedding service.
2. Pre-RAG explicit language switch detection & session preservation.
3. Multi-business Agent RAG strict isolation (School vs Hospital vs Salon).
4. Dynamic entity extraction (doctor, teacher, stylist, dates, names, contacts).
5. Single-greeting guarantee and conversation state management.
6. Universal Local Voice Test execution without hardcoded domains.
"""
import pytest
import pytest_asyncio
import numpy as np
import json
import uuid
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.database import Base
from app.db.models import Business, AIAgent, KnowledgeDocument, KnowledgeChunk, Campaign
from app.services.ai.rag.embedding_service import EmbeddingService
from app.services.ai.rag.retrieval_service import AgentRAGRetrievalService
from app.services.ai.conversation.memory_service import ConversationMemoryService
from app.services.ai.agent_engine import AgentEngine
from app.services.language_service import (
    detect_explicit_language_switch_request,
    detect_speech_language,
)
from app.api.routes.local_voice_test import (
    extract_script_stages,
    start_local_test,
    send_local_test_message,
    StartSessionRequest,
    MessageRequest,
)


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


# ---------------------------------------------------------------------------
# Test 1: Embedding Service 384-dim & Normalization
# ---------------------------------------------------------------------------
def test_embedding_service_384_dim_and_normalization():
    text = "CallZenza dynamic conversational AI for multi-business agents"
    vec = EmbeddingService.generate_embedding(text)

    assert isinstance(vec, list)
    assert len(vec) == 384
    norm = np.linalg.norm(vec)
    assert np.isclose(norm, 1.0, atol=1e-3), f"Vector norm was {norm}, expected ~1.0"

    sim_self = EmbeddingService.cosine_similarity(vec, vec)
    assert np.isclose(sim_self, 1.0, atol=1e-4)

    # Batch embeddings
    batch_vecs = EmbeddingService.generate_batch_embeddings(["Hello world", "வணக்கம்", "नमस्ते"])
    assert len(batch_vecs) == 3
    for b in batch_vecs:
        assert len(b) == 384
        assert np.isclose(np.linalg.norm(b), 1.0, atol=1e-3)


# ---------------------------------------------------------------------------
# Test 2: Explicit Pre-RAG Language Switching
# ---------------------------------------------------------------------------
def test_pre_rag_language_switch_detection():
    # Tamil queries
    ta_query_1 = "Can you talk in Tamil please?"
    ta_res_1 = detect_explicit_language_switch_request(ta_query_1)
    assert ta_res_1 is not None
    assert ta_res_1[0] == "ta"

    ta_query_2 = "தமிழில் பேசுங்கள்"
    ta_res_2 = detect_explicit_language_switch_request(ta_query_2)
    assert ta_res_2 is not None
    assert ta_res_2[0] == "ta"

    # English queries
    en_query = "Please speak in English"
    en_res = detect_explicit_language_switch_request(en_query)
    assert en_res is not None
    assert en_res[0] == "en"

    # Hindi queries
    hi_query = "Hindi mein baat karo please"
    hi_res = detect_explicit_language_switch_request(hi_query)
    assert hi_res is not None
    assert hi_res[0] == "hi"

    # Telugu query
    te_query = "Please switch to Telugu"
    te_res = detect_explicit_language_switch_request(te_query)
    assert te_res is not None
    assert te_res[0] == "te"

    # Non-switch business questions must return None
    biz_q1 = "What are the school fees for Grade 5?"
    assert detect_explicit_language_switch_request(biz_q1) is None

    biz_q2 = "Is Dr. Kumar available tomorrow?"
    assert detect_explicit_language_switch_request(biz_q2) is None


# ---------------------------------------------------------------------------
# Test 3: Language Switching Preserves Session Memory & Entities
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_language_switch_preserves_memory(db_session: AsyncSession):
    agent_id = f"agent_{uuid.uuid4().hex[:8]}"
    session_id = f"sess_{uuid.uuid4().hex[:8]}"

    biz = Business(id=f"biz_{uuid.uuid4().hex[:8]}", name="Acme Corp", owner_user_id="user_owner_1")
    ai_agent = AIAgent(
        id=agent_id,
        business_id=biz.id,
        name="Universal Assistant",
        system_prompt="You are a helpful customer assistant.",
        language="en",
    )
    db_session.add_all([biz, ai_agent])
    await db_session.commit()

    # Turn 1: User provides intent and appointment time in English
    res1 = await AgentEngine.process_user_query(
        db=db_session,
        agent_id=agent_id,
        message="I would like to book a consultation with a specialist tomorrow morning",
        session_id=session_id,
        current_language="en",
    )
    assert res1.language == "en"
    entities = ConversationMemoryService.get_entities(agent_id, session_id)
    assert entities.get("date") == "tomorrow"
    assert entities.get("time") == "morning"
    assert entities.get("target_entity") == "specialist"

    # Turn 2: User explicitly requests Tamil
    res2 = await AgentEngine.process_user_query(
        db=db_session,
        agent_id=agent_id,
        message="Can you talk in Tamil please?",
        session_id=session_id,
        current_language="en",
    )
    assert res2.language == "ta"
    assert "தமிழில்" in res2.response

    # Verify conversation state and entities were completely preserved
    entities_after = ConversationMemoryService.get_entities(agent_id, session_id)
    assert entities_after.get("date") == "tomorrow"
    assert entities_after.get("time") == "morning"
    assert entities_after.get("target_entity") == "specialist"
    assert ConversationMemoryService.get_language(agent_id, session_id) == "ta"


# ---------------------------------------------------------------------------
# Test 4: Strict Multi-Tenant & Multi-Business Agent RAG Isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_multi_business_agent_rag_isolation(db_session: AsyncSession):
    # Setup 3 different businesses with distinct domains
    biz_school = Business(id="biz_school_101", name="Greenwood High School", owner_user_id="user_owner_school")
    agent_school = AIAgent(
        id="agent_school_101",
        business_id=biz_school.id,
        name="School Admissions Officer",
        system_prompt="You are an admissions assistant for Greenwood High.",
        language="en",
    )

    biz_hospital = Business(id="biz_hospital_202", name="Metro Health Clinic", owner_user_id="user_owner_hospital")
    agent_hospital = AIAgent(
        id="agent_hospital_202",
        business_id=biz_hospital.id,
        name="Clinic Receptionist",
        system_prompt="You are a medical assistant for Metro Health.",
        language="en",
    )

    biz_salon = Business(id="biz_salon_303", name="Glow Hair Studio", owner_user_id="user_owner_salon")
    agent_salon = AIAgent(
        id="agent_salon_303",
        business_id=biz_salon.id,
        name="Salon Stylist Agent",
        system_prompt="You are a styling assistant for Glow Hair Studio.",
        language="en",
    )

    db_session.add_all([biz_school, agent_school, biz_hospital, agent_hospital, biz_salon, agent_salon])
    await db_session.commit()

    # Ingest Knowledge Documents and Chunks for each agent
    doc_school = KnowledgeDocument(id="doc_school", business_id=biz_school.id, agent_id=agent_school.id, title="School Fees")
    chunk_school = KnowledgeChunk(
        id="chunk_school_1",
        document_id=doc_school.id,
        business_id=biz_school.id,
        agent_id=agent_school.id,
        version_id="v_school_1",
        is_active=True,
        text="Montessori primary school admission fee is $500 per term. Annual sports fee is $100.",
        embedding=json.dumps(EmbeddingService.generate_embedding("Montessori primary school admission fee $500 term sports $100")),
    )

    doc_hospital = KnowledgeDocument(id="doc_hospital", business_id=biz_hospital.id, agent_id=agent_hospital.id, title="Clinic Fees")
    chunk_hospital = KnowledgeChunk(
        id="chunk_hospital_1",
        document_id=doc_hospital.id,
        business_id=biz_hospital.id,
        agent_id=agent_hospital.id,
        version_id="v_hospital_1",
        is_active=True,
        text="Cardiology doctor consultation fee is $150. General physician fee is $50.",
        embedding=json.dumps(EmbeddingService.generate_embedding("Cardiology doctor consultation fee $150 general physician $50")),
    )

    doc_salon = KnowledgeDocument(id="doc_salon", business_id=biz_salon.id, agent_id=agent_salon.id, title="Salon Packages")
    chunk_salon = KnowledgeChunk(
        id="chunk_salon_1",
        document_id=doc_salon.id,
        business_id=biz_salon.id,
        agent_id=agent_salon.id,
        version_id="v_salon_1",
        is_active=True,
        text="Hair styling and coloring package is $80. Spa manicure is $35.",
        embedding=json.dumps(EmbeddingService.generate_embedding("Hair styling coloring package $80 spa manicure $35")),
    )

    db_session.add_all([doc_school, chunk_school, doc_hospital, chunk_hospital, doc_salon, chunk_salon])
    await db_session.commit()

    # 1. Query School Agent for "fees"
    school_chunks = await AgentRAGRetrievalService.retrieve_agent_knowledge(
        db=db_session,
        agent_id=agent_school.id,
        query="What is the admission fee for Montessori school?",
        business_id=biz_school.id,
    )
    assert len(school_chunks) > 0
    assert "Montessori" in school_chunks[0]["text"]
    assert "Cardiology" not in school_chunks[0]["text"]
    assert "Hair styling" not in school_chunks[0]["text"]

    # 2. Query Hospital Agent for "doctor consultation fee"
    hospital_chunks = await AgentRAGRetrievalService.retrieve_agent_knowledge(
        db=db_session,
        agent_id=agent_hospital.id,
        query="How much is the cardiology doctor consultation fee?",
        business_id=biz_hospital.id,
    )
    assert len(hospital_chunks) > 0
    assert "Cardiology doctor" in hospital_chunks[0]["text"]
    assert "Montessori" not in hospital_chunks[0]["text"]
    assert "Hair styling" not in hospital_chunks[0]["text"]

    # 3. Query Salon Agent for "package price"
    salon_chunks = await AgentRAGRetrievalService.retrieve_agent_knowledge(
        db=db_session,
        agent_id=agent_salon.id,
        query="What is the hair styling package rate?",
        business_id=biz_salon.id,
    )
    assert len(salon_chunks) > 0
    assert "Hair styling" in salon_chunks[0]["text"]
    assert "Montessori" not in salon_chunks[0]["text"]
    assert "Cardiology" not in salon_chunks[0]["text"]


# ---------------------------------------------------------------------------
# Test 5: Dynamic Entity Extraction Across Diverse Industries
# ---------------------------------------------------------------------------
def test_dynamic_entity_extraction():
    agent_id = "test_agent_entities"
    sess_doctor = "sess_doctor_001"
    sess_teacher = "sess_teacher_002"
    sess_salon = "sess_salon_003"

    # Healthcare
    ConversationMemoryService.clear_session(agent_id, sess_doctor)
    ConversationMemoryService.add_turn(
        agent_id,
        sess_doctor,
        role="user",
        content="I want to consult with a cardiologist tomorrow at 3pm. My name is Ramesh."
    )
    doc_entities = ConversationMemoryService.get_entities(agent_id, sess_doctor)
    assert doc_entities.get("target_entity") == "cardiologist"
    assert doc_entities.get("date") == "tomorrow"
    assert doc_entities.get("time") == "3pm"
    assert doc_entities.get("name") == "Ramesh"

    # Education
    ConversationMemoryService.clear_session(agent_id, sess_teacher)
    ConversationMemoryService.add_turn(
        agent_id,
        sess_teacher,
        role="user",
        content="I would like to meet with a teacher next monday morning. My name is Sarah."
    )
    edu_entities = ConversationMemoryService.get_entities(agent_id, sess_teacher)
    assert edu_entities.get("target_entity") == "teacher"
    assert edu_entities.get("date") == "next monday"
    assert edu_entities.get("time") == "morning"
    assert edu_entities.get("name") == "Sarah"

    # Salon
    ConversationMemoryService.clear_session(agent_id, sess_salon)
    ConversationMemoryService.add_turn(
        agent_id,
        sess_salon,
        role="user",
        content="Can I book with a stylist today evening? Email is priya@gmail.com"
    )
    salon_entities = ConversationMemoryService.get_entities(agent_id, sess_salon)
    assert salon_entities.get("target_entity") == "stylist"
    assert salon_entities.get("date") == "today"
    assert salon_entities.get("time") == "evening"
    assert salon_entities.get("email") == "priya@gmail.com"


# ---------------------------------------------------------------------------
# Test 6: Single-Greeting Guarantee
# ---------------------------------------------------------------------------
def test_single_greeting_guarantee():
    agent_id = "test_agent_greeting"
    session_id = "sess_greeting_1"

    ConversationMemoryService.clear_session(agent_id, session_id)
    assert ConversationMemoryService.is_greeting_sent(agent_id, session_id) is False

    ConversationMemoryService.mark_greeting_sent(agent_id, session_id)
    assert ConversationMemoryService.is_greeting_sent(agent_id, session_id) is True


# ---------------------------------------------------------------------------
# Test 7: Universal Local Voice Test Route Parity
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_universal_local_voice_test_parity(db_session: AsyncSession):
    biz = Business(id="biz_real_estate", name="Skyline Realty", owner_user_id="user_owner_realty")
    agent = AIAgent(
        id="agent_skyline",
        business_id=biz.id,
        name="Sarah",
        system_prompt="You are a real estate assistant for Skyline Realty.",
        language="en",
    )
    script_content = "1. Greeting: Hello! I am Sarah from Skyline Realty.\n2. Property Preference: What type of apartment are you looking for?\n3. Site Visit: Would you like to schedule a site visit this weekend?"
    campaign = Campaign(
        id="cmp_real_estate",
        business_id=biz.id,
        agent_id=agent.id,
        name="Skyline Luxury Apartments",
        schedule={"script": script_content},
    )
    db_session.add_all([biz, agent, campaign])
    await db_session.commit()

    # 1. Start local voice test session
    start_req = StartSessionRequest(campaign_id=campaign.id)
    start_resp = await start_local_test(start_req, db=db_session)
    assert "session_id" in start_resp
    assert start_resp["campaign_name"] == "Skyline Luxury Apartments"
    assert "Sarah" in start_resp["greeting"]
    assert len(start_resp["script_steps"]) >= 3
    assert start_resp["script_steps"][0]["name"].startswith("1. Greeting")

    # 2. Send message
    msg_req = MessageRequest(
        session_id=start_resp["session_id"],
        user_message="I am looking for a 3 BHK apartment and would like to visit tomorrow morning",
    )
    msg_resp = await send_local_test_message(msg_req, db=db_session)
    assert "response" in msg_resp
    assert msg_resp["collected_data"].get("date") == "tomorrow"
    assert msg_resp["collected_data"].get("time") == "morning"
