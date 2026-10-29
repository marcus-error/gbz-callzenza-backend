"""
Test Suite: AI Agent Qualification Lifecycle, DND Memory & Call Next Lead

Verifies:
1. Multi-lingual DND Intent Detection (English, Tamil, Tanglish, Hindi).
2. DND Suppression and Persistence (dnd_registry and Lead table).
3. Step-by-Step Dynamic Script Qualification Flow (Ordered execution without skipping).
4. Service / Appointment Confirmation Trigger on full qualification.
5. Permission Before Hanging Up ("Is there anything else I can help you with, or may I end the call?").
6. Caller Permission Granted -> Warm farewell, terminal_hangup=True, disposition=QUALIFIED.
7. Caller Questions After Confirmation -> Answered without hanging up (terminal_hangup=False).
8. Immediate DND Hangup during call -> terminal_hangup=True, is_dnd=True, disposition=DNC.
9. Outbound Dialing Suppression for DND numbers.
"""

import asyncio
import os
import sys
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, DoNotCallRecord, Lead, CampaignLead, Campaign, Business
from app.services.dnd_service import (
    canonical_phone_key,
    detect_dnd_intent,
    is_number_suppressed,
    add_to_dnd,
)
from app.services.ai.agent_engine import (
    AgentEngine,
    AgentEngineResponse,
)
from app.services.ai.conversation.memory_service import ConversationMemoryService


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_dnd_multilingual_intent_detection():
    """Verify DND detection across English, Tamil, Tanglish, and Hindi."""
    # English
    assert detect_dnd_intent("Please stop calling me") is True
    assert detect_dnd_intent("Put me on your do not call list") is True
    assert detect_dnd_intent("Don't ever call this number again") is True
    assert detect_dnd_intent("Remove my number from your database") is True

    # Tamil / Tanglish
    assert detect_dnd_intent("Enakku call panadhinga") is True
    assert detect_dnd_intent("Inime call pannatheenga") is True
    assert detect_dnd_intent("Call seiyadheergal please") is True

    # Hindi
    assert detect_dnd_intent("Mujhe call mat karo") is True
    assert detect_dnd_intent("Dobara phone mat karna") is True
    assert detect_dnd_intent("Mera number list se hata do") is True

    # Non-DND phrases should NOT trigger DND
    assert detect_dnd_intent("Yes, I am interested in the product") is False
    assert detect_dnd_intent("Can you call me back tomorrow at 5pm?") is False
    assert detect_dnd_intent("I am 32 years old") is False
    assert detect_dnd_intent("Sure, please go ahead") is False


def test_intent_classification_for_dnd_and_permission_to_end():
    """Verify intent classification accurately categorizes DND and call ending permissions."""
    assert AgentEngine.classify_caller_intent("Please stop calling me") == "dnd_request"
    assert AgentEngine.classify_caller_intent("Mujhe call mat karo") == "dnd_request"
    assert AgentEngine.classify_caller_intent("Inime call pannatheenga") == "dnd_request"
    assert AgentEngine.classify_caller_intent("No more questions, thank you, bye!") == "permission_to_end_call"
    assert AgentEngine.classify_caller_intent("You can end the call") == "permission_to_end_call"
    assert AgentEngine.classify_caller_intent("That is all, goodbye") == "permission_to_end_call"
    assert AgentEngine.classify_caller_intent("Nothing else, thanks bye") == "permission_to_end_call"


def test_canonical_phone_normalization():
    """Verify phone normalization handles +91, 0, whitespace, hyphens."""
    assert canonical_phone_key("+91 98765-43210") == "9876543210"
    assert canonical_phone_key("09876543210") == "9876543210"
    assert canonical_phone_key("919876543210") == "9876543210"
    assert canonical_phone_key("9876543210") == "9876543210"


@pytest.mark.asyncio
async def test_dnd_database_persistence_and_suppression():
    """Verify adding to DND persists in dnd_registry and suppresses future calls."""
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        biz = Business(id="biz_test_01", name="Test Business", owner_user_id="user_test_01")
        lead = Lead(
            id="lead_test_01",
            business_id="biz_test_01",
            phone="+919876543210",
            first_name="Ramesh",
            status="PENDING",
            call_status="PENDING"
        )
        db.add_all([biz, lead])
        await db.commit()

        # Check initial state - not suppressed
        suppressed = await is_number_suppressed(db, "+919876543210", "biz_test_01")
        assert suppressed is False

        # Register DND
        dnd_record = await add_to_dnd(
            db,
            phone="+919876543210",
            business_id="biz_test_01",
            lead_id="lead_test_01",
            reason="Caller requested do not call during test"
        )
        assert dnd_record is not None

        # Verify suppression is now active across multiple representations
        assert await is_number_suppressed(db, "9876543210", "biz_test_01") is True
        assert await is_number_suppressed(db, "+91 98765 43210", "biz_test_01") is True
        assert await is_number_suppressed(db, "09876543210", "biz_test_01") is True

        # Verify lead status was updated to DNC
        await db.refresh(lead)
        assert lead.status == "DNC"
        assert lead.call_status == "DNC"
        assert lead.call_result == "Do Not Call (DNC)"

    await engine.dispose()


@pytest.mark.asyncio
async def test_ai_agent_dnd_interception_lifecycle():
    """Verify that when caller says DND during call, engine immediately returns polite DND response, terminal_hangup=True, is_dnd=True."""
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    agent_id = "test_agent_dnd"
    session_id = str(uuid.uuid4())

    async with async_session() as db:
        response: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="Please don't call me again, put me on DND!",
            session_id=session_id,
            is_voice=True
        )

        assert response.terminal_hangup is True
        assert response.is_dnd is True
        assert response.call_disposition == "DNC"
        assert "not call" in response.response_text.lower() or "dnd" in response.response_text.lower() or "apologize" in response.response_text.lower()

    await engine.dispose()


@pytest.mark.asyncio
async def test_complete_qualification_confirmation_permission_and_hangup():
    """
    Verify complete qualification flow:
    Turn A: All stages satisfied -> System proposes service confirmation
    Turn B: Caller confirms service -> System asks permission: 'Is there anything else I can help you with today, or may I end the call?'
    Turn C: Caller grants permission -> System returns warm farewell with terminal_hangup=True, disposition=QUALIFIED
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    agent_id = "test_agent_lifecycle"
    session_id = str(uuid.uuid4())
    script_solar = """
    Stage 1: Homeowner | Question: Are you an independent homeowner? | Field: homeowner
    Stage 2: Bill | Question: What is your average electricity bill? | Field: monthly_bill
    """

    async with async_session() as db:
        # Pre-populate collected entities to satisfy all stages
        ConversationMemoryService.set_entity(agent_id, session_id, "homeowner", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "home_ownership", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "property_type", "single_family")
        ConversationMemoryService.set_entity(agent_id, session_id, "bill", "4500")
        ConversationMemoryService.set_entity(agent_id, session_id, "monthly_bill", "4500")
        ConversationMemoryService.set_eligibility(agent_id, session_id, status="QUALIFIED")

        # Turn A: Qualification satisfied -> System proposes service confirmation
        res_confirm: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="Yes, I have an independent house and my electricity bill is 4000 rupees.",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_confirm.terminal_hangup is False
        assert res_confirm.call_disposition == "QUALIFIED"
        assert "confirm" in res_confirm.response_text.lower() or "qualified" in res_confirm.response_text.lower() or "booking" in res_confirm.response_text.lower()

        # Turn B: Caller confirms service -> System asks permission before hanging up
        res_perm: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="Yes, please go ahead and confirm the booking.",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_perm.terminal_hangup is False
        assert "anything else" in res_perm.response_text.lower() or "may i end the call" in res_perm.response_text.lower() or "help you with" in res_perm.response_text.lower()

        # Turn C: Caller grants permission -> System returns warm farewell with terminal_hangup=True
        res_bye: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="No more questions, thank you so much! Bye bye.",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_bye.terminal_hangup is True
        assert res_bye.is_dnd is False
        assert res_bye.call_disposition == "QUALIFIED"
        assert "welcome" in res_bye.response_text.lower() or "day" in res_bye.response_text.lower() or "goodbye" in res_bye.response_text.lower() or "thank" in res_bye.response_text.lower()

    await engine.dispose()


@pytest.mark.asyncio
async def test_caller_asks_additional_question_after_confirmation():
    """
    Verify that if the caller asks a question after service confirmation instead of granting permission to hang up,
    the engine answers the question and does NOT hang up.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    agent_id = "test_agent_question"
    session_id = str(uuid.uuid4())
    script_solar = """
    Stage 1: Homeowner | Question: Are you an independent homeowner? | Field: homeowner
    Stage 2: Bill | Question: What is your average electricity bill? | Field: monthly_bill
    """

    async with async_session() as db:
        ConversationMemoryService.set_entity(agent_id, session_id, "homeowner", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "home_ownership", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "property_type", "single_family")
        ConversationMemoryService.set_entity(agent_id, session_id, "bill", "4500")
        ConversationMemoryService.set_entity(agent_id, session_id, "monthly_bill", "4500")
        ConversationMemoryService.set_eligibility(agent_id, session_id, status="QUALIFIED")
        state = ConversationMemoryService.get_session_state(agent_id, session_id)
        state["service_confirmation_asked"] = True
        state["end_call_permission_asked"] = True

        res_q: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="Wait, will your engineer call me before coming to my house?",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_q.terminal_hangup is False
        updated_state = ConversationMemoryService.get_session_state(agent_id, session_id)
        assert updated_state.get("end_call_permission_asked") is False

    await engine.dispose()


@pytest.mark.asyncio
async def test_caller_declines_service_confirmation():
    """
    Verify that if the caller declines service confirmation (e.g. 'No, I am not interested at all'),
    the engine marks disposition as NOT_INTERESTED, records service_confirmed=False,
    asks permission to end the call, and does NOT enter an infinite loop.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    agent_id = "test_agent_decline"
    session_id = str(uuid.uuid4())
    script_solar = """
    Stage 1: Homeowner | Question: Are you an independent homeowner? | Field: homeowner
    Stage 2: Bill | Question: What is your average electricity bill? | Field: monthly_bill
    """

    async with async_session() as db:
        ConversationMemoryService.set_entity(agent_id, session_id, "homeowner", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "home_ownership", "owner")
        ConversationMemoryService.set_entity(agent_id, session_id, "property_type", "single_family")
        ConversationMemoryService.set_entity(agent_id, session_id, "bill", "4500")
        ConversationMemoryService.set_entity(agent_id, session_id, "monthly_bill", "4500")
        ConversationMemoryService.set_eligibility(agent_id, session_id, status="QUALIFIED")
        state = ConversationMemoryService.get_session_state(agent_id, session_id)
        state["service_confirmation_asked"] = True

        res_decline: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="No, I am not interested at all.",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_decline.terminal_hangup is False
        assert res_decline.call_disposition == "NOT_INTERESTED"
        updated_state = ConversationMemoryService.get_session_state(agent_id, session_id)
        assert updated_state.get("service_confirmed") is False
        assert updated_state.get("end_call_permission_asked") is True

        # Now when caller says "No, that's all, goodbye", it should cleanly end call with NOT_INTERESTED
        res_end: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="No, that is all. Goodbye.",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res_end.terminal_hangup is True
        assert res_end.call_disposition == "NOT_INTERESTED"

    await engine.dispose()


@pytest.mark.asyncio
async def test_direct_goodbye_permission_to_end_call():
    """
    Verify that an unprompted or direct goodbye ('You can end the call now, goodbye') immediately
    triggers terminal_hangup=True without requiring an intermediate confirmation prompt.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    agent_id = "test_agent_direct_bye"
    session_id = str(uuid.uuid4())
    script_solar = """
    Stage 1: Homeowner | Question: Are you an independent homeowner? | Field: homeowner
    """

    async with async_session() as db:
        res: AgentEngineResponse = await AgentEngine.process_user_query(
            db=db,
            agent_id=agent_id,
            message="You can end the call now, goodbye!",
            session_id=session_id,
            script_text=script_solar,
            is_voice=True
        )
        assert res.terminal_hangup is True
        assert "goodbye" in res.response_text.lower() or "day" in res.response_text.lower() or "welcome" in res.response_text.lower()

    await engine.dispose()


@pytest.mark.asyncio
async def test_strict_dnd_pre_dial_suppression_enforcement():
    """
    Verify that is_number_suppressed accurately catches both dnd_registry entries
    and marked Lead table entries, ensuring strict pre-dial SIP suppression.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        biz = Business(id="biz_pre_dial", name="PreDial Biz", owner_user_id="user_pre_dial")
        db.add(biz)
        await db.commit()

        # Add number to DND registry
        await add_to_dnd(
            db=db,
            phone="+91 91234 56789",
            business_id="biz_pre_dial",
            reason="Customer requested Do Not Call",
        )
        await db.commit()

        # Check various representations of the exact same number
        assert await is_number_suppressed(db, "+919123456789", "biz_pre_dial") is True
        assert await is_number_suppressed(db, "09123456789", "biz_pre_dial") is True
        assert await is_number_suppressed(db, "9123456789", "biz_pre_dial") is True
        assert await is_number_suppressed(db, "919123456789", "biz_pre_dial") is True

        # Unrelated number is NOT suppressed
        assert await is_number_suppressed(db, "+919876543210", "biz_pre_dial") is False

    await engine.dispose()

