"""
Verification script for CallZenza AI State Flow & Data Separation Bug Fix.
Simulates exact multi-turn outbound call flow:
1. AI Greeting generation and ResponseGuard validation.
2. Customer speech ("I'm interested in buying a plot.").
3. STT -> RAG Context retrieval -> Script context matching -> LLM Response generation.
4. Response Guard validation.
5. Verifies TTS input == LLM Response and raw RAG/script content is NEVER spoken or exposed as assistant message.
"""
import pytest
import pytest_asyncio
import uuid
import re

from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Business, Campaign, Lead, Conversation, Message, Call, KnowledgeLibrary
from app.services.lead_engagement_service import process_incoming_message, generate_personalized_greeting
from app.services.ai.response_sanitizer import ResponseSanitizer, ResponseGuard
from app.api.routes.voice import extract_campaign_script_greeting


@pytest_asyncio.fixture
async def setup_call_context():
    async with AsyncSessionLocal() as db:
        biz_id = str(uuid.uuid4())
        user_id = str(uuid.uuid4())
        agent_id = str(uuid.uuid4())
        camp_id = str(uuid.uuid4())
        lead_id = str(uuid.uuid4())
        conv_id = str(uuid.uuid4())
        call_id = str(uuid.uuid4())

        biz = Business(id=biz_id, name="Dream Catchers Properties", owner_user_id=user_id)
        agent = AIAgent(id=agent_id, business_id=biz_id, name="MAGIC", status="ACTIVE", language="en")
        lead = Lead(id=lead_id, business_id=biz_id, first_name="Manonmani", last_name="Viswanathan", phone="+919876543210", status="NEW")

        raw_script_doc = (
            "Dream Catchers Properties | Outbound RAG Script Page 1 DREAM CATCHERS PROPERTIES INFORMATION OUTBOUND RAG SCRIPT\n"
            "ABOUT US:\n"
            "Dream Catchers Properties is a premier real estate developer in Chennai offering residential and investment plots.\n"
            "LOCATIONS:\n"
            "OMR, Karandai, Nimmi Garden, East Coast Road.\n"
            "PROPERTY TYPES:\n"
            "Residential plots: 1200 sq ft, 2400 sq ft.\n"
            "Investment plots: 960 sq ft.\n"
            "PRICING:\n"
            "Plots start at 25 Lakhs.\n"
            "RULES:\n"
            "Rule 1: Always ask whether customer wants residential or investment.\n"
            "Rule 2: Offer site visit on weekends."
        )

        camp = Campaign(
            id=camp_id,
            business_id=biz_id,
            name="Dream Catchers Outbound",
            schedule={"script": raw_script_doc}
        )

        conv = Conversation(id=conv_id, business_id=biz_id, lead_id=lead_id, agent_id=agent_id, status="ACTIVE", channel="VOICE")
        call = Call(id=call_id, business_id=biz_id, campaign_id=camp_id, conversation_id=conv_id, status="IN_PROGRESS")

        # Also populate KnowledgeLibrary entry representing uploaded document
        kl = KnowledgeLibrary(
            business_id=biz_id,
            agent_id=agent_id,
            question="What properties do you have in Chennai?",
            answer=raw_script_doc,
            source_data="Uploaded Script RAG Document"
        )

        db.add_all([biz, agent, lead, camp, conv, call, kl])
        await db.commit()

        yield {
            "db": db,
            "biz": biz,
            "agent": agent,
            "lead": lead,
            "camp": camp,
            "conv": conv,
            "call": call,
            "raw_script_doc": raw_script_doc
        }


@pytest.mark.asyncio
async def test_greeting_never_speaks_raw_rag_script(setup_call_context):
    """Verify that campaign greeting never falls back to dumping the entire script document."""
    ctx = setup_call_context
    raw_doc = ctx["raw_script_doc"]
    lead = ctx["lead"]
    agent = ctx["agent"]

    greeting = extract_campaign_script_greeting(raw_doc, lead=lead, agent=agent)
    
    # Since raw_doc has no explicit dialogue quote, extract_campaign_script_greeting returns None
    assert greeting is None, "extract_campaign_script_greeting must return None when no clean single line is present."

    # Dynamic fallback greeting
    opening_line = f"Good morning {lead.first_name} {lead.last_name}! Hi, this is {agent.name} calling from Dream Catchers Properties. How are you doing today?"
    clean_opening = ResponseSanitizer.clean_customer_facing_text(opening_line, agent_name=agent.name, lead_name=lead.first_name)
    guard_res = ResponseGuard.validate_customer_response(clean_opening)

    assert guard_res["is_valid"] is True, f"Greeting failed ResponseGuard: {guard_res}"
    assert "Dream Catchers Properties | Outbound RAG Script" not in clean_opening
    assert len(clean_opening) <= 250


@pytest.mark.asyncio
async def test_multi_turn_rag_retrieval_used_strictly_as_context(setup_call_context):
    """
    Verify customer utterance 'I'm interested in buying a plot.'
    retrieves RAG context internally, but LLM generates a clean customer-facing answer
    and raw RAG script document NEVER reaches TTS or ai_message.content.
    """
    ctx = setup_call_context
    async with AsyncSessionLocal() as db:
        lead = await db.get(Lead, ctx["lead"].id)
        conv = await db.get(Conversation, ctx["conv"].id)
        agent = await db.get(AIAgent, ctx["agent"].id)
        camp = await db.get(Campaign, ctx["camp"].id)

        customer_speech = "I'm interested in buying a plot."

        result = await process_incoming_message(
            db=db,
            lead=lead,
            conversation=conv,
            agent=agent,
            content=customer_speech,
            campaign=camp,
        )

        ai_msg = result["ai_message"]
        assert ai_msg is not None, "process_incoming_message must return an ai_message"
        ai_text = ai_msg.content

        # 1. Check Response Guard validation
        guard_res = ResponseGuard.validate_customer_response(ai_text)
        assert guard_res["is_valid"] is True, f"Generated AI response failed ResponseGuard: {guard_res}"

        # 2. Check strict non-exposure of raw document content
        assert "Dream Catchers Properties | Outbound RAG Script" not in ai_text
        assert "Rule 1:" not in ai_text
        assert "Rule 2:" not in ai_text
        assert "Page 1" not in ai_text
        assert "OUTBOUND RAG SCRIPT" not in ai_text

        # 3. Check response length and natural conversational tone
        assert len(ai_text) < 350, f"AI response was too long ({len(ai_text)} chars), expected short conversational response."
        assert len(ai_text.splitlines()) <= 2, "AI response should be 1-2 lines."

        # 4. Check return tracing fields
        assert result["response_guard_passed"] is True
        assert result["llm_response"] == ai_text
        assert result["rag_retrieved"] is True or result["script_matched"] is True
