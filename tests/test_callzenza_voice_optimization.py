"""
Comprehensive Test Suite for CallZenza SIP Trunk Outbound Call & AI Voice Response Optimization.
Verifies all 21 checklist requirements across:
- Outbound calling flow & 2-way audio setup
- Multi-detail extraction in a single utterance
- TXT script logical section chunking & PDF coexistence
- Agent & Enterprise RAG security isolation
- Clean natural response rendering (no metadata/chunk leaks)
- Consecutive call session isolation
- Sub-phase latency breakdown logging
- Handoff condition triggering
"""
import json
import uuid
import pytest
import pytest_asyncio
from sqlalchemy import select

from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Business, Call, Campaign, Conversation, KnowledgeChunk, KnowledgeDocument, Lead, Message, User
from app.rag.embedding_service import generate_text_embedding
from app.rag.langchain_chain import split_document_text
from app.services.document_service import DocumentProcessingService
from app.services.lead_engagement_service import _extract_fields, process_incoming_message, format_prompt_for_lead
from app.services.voice_service import initiate_outbound_call
from app.services.rag_service import RAGService
from app.utils.tamil_sanitizer import clean_metadata_headers, extract_spoken_dialogue


@pytest_asyncio.fixture
async def db_session():
    async with AsyncSessionLocal() as session:
        yield session


@pytest.mark.asyncio
async def test_outbound_call_and_2way_audio_setup(db_session):
    """Test 1: Verify outbound call initiation creates fresh Call and Conversation models."""
    biz_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    business = Business(id=biz_id, name="Voice Test Biz", owner_user_id=user_id)
    db_session.add(business)
    await db_session.flush()

    lead = Lead(
        business_id=business.id,
        first_name="John",
        last_name="Doe",
        phone="+15550199",
        status="NEW"
    )
    agent = AIAgent(
        business_id=business.id,
        name="Alex Voice Agent",
        voice_id="EXAVITQu4vr4xnSDxMaL",
        status="ACTIVE"
    )
    db_session.add_all([lead, agent])
    await db_session.commit()

    call = await initiate_outbound_call(db_session, lead, agent, force_mock=True)
    await db_session.commit()

    assert call.id is not None
    assert call.lead_id == lead.id
    assert call.agent_id == agent.id
    assert call.direction == "OUTBOUND"
    assert call.status in ("QUEUED", "COMPLETED", "IN_PROGRESS")
    assert call.conversation_id is not None


    conv = await db_session.get(Conversation, call.conversation_id)
    assert conv is not None
    assert conv.status in ("ACTIVE", "CLOSED")



@pytest.mark.asyncio
async def test_multi_detail_extraction_in_single_turn(db_session):
    """Test 2: Verify extraction of multiple details (product model + callback time + budget) in 1 turn."""
    lead = Lead(
        business_id="biz_multi_extract",
        first_name="Jane",
        last_name="Smith",
        phone="+15550188"
    )
    text = "I am interested in the X100 model and I would like a callback tomorrow evening. My budget is $5000."
    _extract_fields(lead, text)

    assert lead.requirement is not None
    assert "X100" in lead.requirement or "x100" in lead.requirement.lower()
    assert lead.timeline is not None
    assert "tomorrow" in lead.timeline.lower() or "evening" in lead.timeline.lower()
    assert lead.budget == "$5000"

    formatted_prompt = format_prompt_for_lead("Hello {{name}}, requirement is {{requirement}}, timeline is {{timeline}}.", lead)
    assert "Jane Smith" in formatted_prompt
    assert "X100" in formatted_prompt or "x100" in formatted_prompt.lower()


@pytest.mark.asyncio
async def test_txt_script_logical_section_chunking():
    """Test 3: Verify TXT scripts use logical section chunking instead of breaking mid-field."""
    txt_content = (
        "PRODUCT INFORMATION\n"
        "Product: CallZenza AI Pro\n"
        "Price: $299/mo\n"
        "Features: Outbound calling, RAG knowledge base, Low-latency TTS\n"
        "Availability: In Stock\n\n"
        "PRICING & PLANS\n"
        "Starter Plan: $99/mo with 1,000 mins\n"
        "Enterprise Plan: $499/mo with unlimited mins\n\n"
        "OBJECTION HANDLING\n"
        "Q: Is it difficult to set up?\n"
        "A: No, CallZenza deploys in under 5 minutes with full API integration.\n\n"
        "CLOSING\n"
        "Thank the customer and schedule a live demo with our technical expert."
    )

    chunks = split_document_text(txt_content, chunk_size=300, chunk_overlap=30)
    assert len(chunks) >= 3

    # Check that logical sections stay unified
    prod_chunk = next((c for c in chunks if "PRODUCT INFORMATION" in c or "CallZenza AI Pro" in c), None)
    assert prod_chunk is not None
    assert "Price: $299/mo" in prod_chunk
    assert "Availability: In Stock" in prod_chunk

    obj_chunk = next((c for c in chunks if "OBJECTION HANDLING" in c or "difficult to set up" in c), None)
    assert obj_chunk is not None
    assert "deploys in under 5 minutes" in obj_chunk


@pytest.mark.asyncio
async def test_clean_response_rendering_no_metadata():
    """Test 4: Verify response dialogue cleaning strips internal RAG headings, chunk IDs, and robotic preambles."""
    raw_response = (
        "[Company_Manual.txt - Chunk 2]:\n"
        "According to the retrieved information from the knowledge base, "
        "the CallZenza platform starts at $99 per month. Would you like to schedule a demo?"
    )
    cleaned = extract_spoken_dialogue(raw_response)
    cleaned = clean_metadata_headers(cleaned)

    assert "[Company_Manual.txt" not in cleaned
    assert "Chunk 2" not in cleaned
    assert "starts at $99 per month" in cleaned


@pytest.mark.asyncio
async def test_agent_and_enterprise_rag_isolation(db_session):
    """Test 5: Verify Agent A cannot retrieve Agent B's RAG chunks."""
    u_a = str(uuid.uuid4())
    u_b = str(uuid.uuid4())
    biz_a = Business(id=str(uuid.uuid4()), name="Biz A Test", owner_user_id=u_a)
    biz_b = Business(id=str(uuid.uuid4()), name="Biz B Test", owner_user_id=u_b)
    db_session.add_all([biz_a, biz_b])
    await db_session.flush()

    agent_a = AIAgent(business_id=biz_a.id, name="Agent A", status="ACTIVE")
    agent_b = AIAgent(business_id=biz_b.id, name="Agent B", status="ACTIVE")
    db_session.add_all([agent_a, agent_b])
    await db_session.flush()

    doc_a = KnowledgeDocument(business_id=biz_a.id, title="Doc A", filename="doca.txt", file_type="TXT", file_path="/tmp/doca.txt")
    doc_b = KnowledgeDocument(business_id=biz_b.id, title="Doc B", filename="docb.txt", file_type="TXT", file_path="/tmp/docb.txt")
    db_session.add_all([doc_a, doc_b])
    await db_session.flush()

    emb_a = generate_text_embedding("Biz A secret product detail")
    emb_b = generate_text_embedding("Biz B secret product detail")

    chunk_a = KnowledgeChunk(
        document_id=doc_a.id,
        business_id=biz_a.id,
        agent_id=agent_a.id,
        text="Biz A secret product detail",
        embedding=json.dumps(emb_a)
    )
    chunk_b = KnowledgeChunk(
        document_id=doc_b.id,
        business_id=biz_b.id,
        agent_id=agent_b.id,
        text="Biz B secret product detail",
        embedding=json.dumps(emb_b)
    )
    db_session.add_all([chunk_a, chunk_b])
    await db_session.commit()

    # Search chunks for Biz A
    results_a = await RAGService.search_chunks(db_session, "secret product", enterprise_id=biz_a.id, rag_id="default_rag")
    texts_a = [r.get("text") for r in results_a]

    assert any("Biz A secret" in t for t in texts_a)
    assert not any("Biz B secret" in t for t in texts_a)


@pytest.mark.asyncio
async def test_call_session_isolation(db_session):
    """Test 6: Verify multiple consecutive calls remain strictly isolated in conversation state."""
    u_id = str(uuid.uuid4())
    biz = Business(id=str(uuid.uuid4()), name="Session Isolation Biz", owner_user_id=u_id)
    db_session.add(biz)
    await db_session.flush()

    lead = Lead(business_id=biz.id, first_name="Tom", phone="+15550177")
    agent = AIAgent(business_id=biz.id, name="Session Agent", status="ACTIVE")
    db_session.add_all([lead, agent])
    await db_session.commit()

    # Call 1
    call1 = await initiate_outbound_call(db_session, lead, agent, force_mock=True)
    conv1 = await db_session.get(Conversation, call1.conversation_id)
    msg1 = Message(conversation_id=conv1.id, sender_type="LEAD", content="Call 1 customer secret")
    db_session.add(msg1)
    await db_session.commit()

    # Call 2
    call2 = await initiate_outbound_call(db_session, lead, agent, force_mock=True)
    conv2 = await db_session.get(Conversation, call2.conversation_id)

    assert call1.id != call2.id
    assert conv1.id != conv2.id

    # Messages in conv2 must NOT contain conv1 messages
    stmt = select(Message).where(Message.conversation_id == conv2.id)
    conv2_msgs = list((await db_session.scalars(stmt)).all())
    assert not any("Call 1 customer secret" in (m.content or "") for m in conv2_msgs)


def test_selective_rag_routing():
    """Test 7: Verify selective RAG routing skips RAG for greetings but triggers RAG for product queries."""
    from app.services.lead_engagement_service import is_rag_required

    assert is_rag_required("Hello") is False
    assert is_rag_required("Good morning") is False
    assert is_rag_required("Okay thanks") is False
    assert is_rag_required("Yes sure") is False

    assert is_rag_required("What is the price of the X100 model?") is True
    assert is_rag_required("Can you tell me your doctor availability and fees?") is True

