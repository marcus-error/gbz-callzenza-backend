"""
Test Suite: Qualification Endpoint NameError Fix & Edge TTS Fallback Verification

Verifies:
1. end_local_test handles session with qualification_status == "QUALIFIED" without NameError.
2. end_local_test handles session with qualification_status == "NOT_QUALIFIED" without NameError.
3. end_local_test handles session with call_disposition == "DNC" / is_dnd == True.
4. end_local_test handles session with missing / None qualification data gracefully.
5. end_local_test handles nonexistent or already-ended session idempotently.
6. Edge TTS generates valid non-empty audio without WebSocket 403.
7. SnapServe fallback to Neural Edge TTS returns valid audio bytes when SnapServe is unavailable.
8. synthesize_for_call returns valid playable audio URL and does not report false success on empty text.
"""

import asyncio
import time
import uuid
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Lead, CampaignLead, Campaign, Business, Call, AIAgent
from app.api.routes.local_voice_test import (
    end_local_test,
    EndSessionRequest,
    local_test_sessions,
)
from app.services.tts_service import (
    synthesize_edge_speech,
    synthesize_snapserve_speech,
    synthesize_for_call,
    reset_snapserve_cooldown,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def test_db_session():
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session

    await engine.dispose()


@pytest.mark.anyio
async def test_end_local_test_qualified(test_db_session: AsyncSession):
    """Verify ending session with qualification_status='QUALIFIED' updates Lead and Call to QUALIFIED."""
    db = test_db_session
    biz = Business(id=str(uuid.uuid4()), name="Test Biz", owner_user_id=str(uuid.uuid4()))
    camp = Campaign(id=str(uuid.uuid4()), business_id=biz.id, name="Test Campaign", agent_type="AI_VOICE")
    lead = Lead(id=str(uuid.uuid4()), business_id=biz.id, phone="919876543210", first_name="John", status="NEW")
    db.add_all([biz, camp, lead])
    await db.commit()

    session_id = f"test-qual-{uuid.uuid4().hex[:8]}"
    local_test_sessions[session_id] = {
        "business_id": biz.id,
        "campaign_id": camp.id,
        "lead_id": lead.id,
        "started_at": time.time() - 30,
        "history": [
            {"role": "assistant", "content": "Hello John"},
            {"role": "user", "content": "Yes, I am interested in the service."},
        ],
        "qualification_status": "QUALIFIED",
        "call_disposition": "QUALIFIED",
    }

    req = EndSessionRequest(session_id=session_id, duration_seconds=30)
    res = await end_local_test(req, db=db)

    assert res["status"] == "success"
    assert res["duration_seconds"] == 30
    assert session_id not in local_test_sessions

    # Check Call in DB
    call = (await db.execute(select(Call).where(Call.lead_id == lead.id))).scalars().first()
    assert call is not None
    assert call.call_result == "QUALIFIED"
    assert call.status == "COMPLETED"

    # Check Lead in DB
    await db.refresh(lead)
    assert lead.status == "QUALIFIED"
    assert lead.qualification_status == "QUALIFIED"


@pytest.mark.anyio
async def test_end_local_test_not_qualified(test_db_session: AsyncSession):
    """Verify ending session with qualification_status='NOT_QUALIFIED' records correct status."""
    db = test_db_session
    biz = Business(id=str(uuid.uuid4()), name="Test Biz", owner_user_id=str(uuid.uuid4()))
    camp = Campaign(id=str(uuid.uuid4()), business_id=biz.id, name="Test Campaign", agent_type="AI_VOICE")
    lead = Lead(id=str(uuid.uuid4()), business_id=biz.id, phone="919876543211", first_name="Sara", status="NEW")
    db.add_all([biz, camp, lead])
    await db.commit()

    session_id = f"test-not-qual-{uuid.uuid4().hex[:8]}"
    local_test_sessions[session_id] = {
        "business_id": biz.id,
        "campaign_id": camp.id,
        "lead_id": lead.id,
        "started_at": time.time() - 20,
        "history": [
            {"role": "assistant", "content": "Hello Sara"},
            {"role": "user", "content": "No, I am not interested."},
        ],
        "qualification_status": "NOT_QUALIFIED",
        "call_disposition": "NOT_QUALIFIED",
    }

    req = EndSessionRequest(session_id=session_id)
    res = await end_local_test(req, db=db)

    assert res["status"] == "success"
    assert session_id not in local_test_sessions

    call = (await db.execute(select(Call).where(Call.lead_id == lead.id))).scalars().first()
    assert call is not None
    assert call.call_result == "NOT_QUALIFIED"

    await db.refresh(lead)
    assert lead.status == "NOT_QUALIFIED"


@pytest.mark.anyio
async def test_end_local_test_dnc_disposition(test_db_session: AsyncSession):
    """Verify ending session with DNC / is_dnd flags marks lead and call as DNC."""
    db = test_db_session
    biz = Business(id=str(uuid.uuid4()), name="Test Biz", owner_user_id=str(uuid.uuid4()))
    camp = Campaign(id=str(uuid.uuid4()), business_id=biz.id, name="Test Campaign", agent_type="AI_VOICE")
    lead = Lead(id=str(uuid.uuid4()), business_id=biz.id, phone="919876543212", first_name="Alex", status="NEW")
    db.add_all([biz, camp, lead])
    await db.commit()

    session_id = f"test-dnc-{uuid.uuid4().hex[:8]}"
    local_test_sessions[session_id] = {
        "business_id": biz.id,
        "campaign_id": camp.id,
        "lead_id": lead.id,
        "started_at": time.time() - 15,
        "history": [
            {"role": "assistant", "content": "Hello Alex"},
            {"role": "user", "content": "Please stop calling me, remove my number."},
        ],
        "is_dnd": True,
        "call_disposition": "DNC",
    }

    req = EndSessionRequest(session_id=session_id)
    res = await end_local_test(req, db=db)

    assert res["status"] == "success"
    call = (await db.execute(select(Call).where(Call.lead_id == lead.id))).scalars().first()
    assert call is not None
    assert call.status == "DNC"
    assert call.call_result == "Do Not Call (DNC)"

    await db.refresh(lead)
    assert lead.status == "DNC"
    assert lead.qualification_status == "DNC"


@pytest.mark.anyio
async def test_end_local_test_missing_qualification_data(test_db_session: AsyncSession):
    """Verify ending session when qualification_status is missing defaults cleanly to COMPLETED/CALLED without NameError."""
    db = test_db_session
    biz = Business(id=str(uuid.uuid4()), name="Test Biz", owner_user_id=str(uuid.uuid4()))
    camp = Campaign(id=str(uuid.uuid4()), business_id=biz.id, name="Test Campaign", agent_type="AI_VOICE")
    lead = Lead(id=str(uuid.uuid4()), business_id=biz.id, phone="919876543213", first_name="Bob", status="NEW")
    db.add_all([biz, camp, lead])
    await db.commit()

    session_id = f"test-missing-qual-{uuid.uuid4().hex[:8]}"
    local_test_sessions[session_id] = {
        "business_id": biz.id,
        "campaign_id": camp.id,
        "lead_id": lead.id,
        "started_at": time.time() - 10,
        "history": [],
        # qualification_status and call_disposition omitted intentionally
    }

    req = EndSessionRequest(session_id=session_id)
    res = await end_local_test(req, db=db)

    assert res["status"] == "success"
    call = (await db.execute(select(Call).where(Call.lead_id == lead.id))).scalars().first()
    assert call is not None
    assert call.status == "COMPLETED"
    assert call.call_result == "COMPLETED"

    await db.refresh(lead)
    assert lead.status == "CALLED"


@pytest.mark.anyio
async def test_end_local_test_idempotent_cleanup(test_db_session: AsyncSession):
    """Verify repeated end calls for nonexistent sessions return 200 without error."""
    db = test_db_session
    req = EndSessionRequest(session_id="non-existent-session-id")
    res = await end_local_test(req, db=db)
    assert res["status"] == "success"
    assert res["duration_seconds"] == 0


@pytest.mark.anyio
async def test_edge_tts_synthesis():
    """Verify Edge TTS generates valid non-empty audio bytes."""
    text = "Hello, this is a test of Edge Text to Speech."
    audio = await synthesize_edge_speech(text, lang="en")
    assert isinstance(audio, bytes)
    assert len(audio) > 1000, f"Expected >1000 bytes from Edge TTS, got {len(audio)}"


@pytest.mark.anyio
async def test_snapserve_fallback_to_edge_tts():
    """Verify SnapServe fallback to Neural Edge TTS works when SnapServe API is unavailable."""
    reset_snapserve_cooldown()
    text = "Testing SnapServe fallback to Neural Edge TTS in CallZenza."
    audio = await synthesize_snapserve_speech(text)
    assert isinstance(audio, bytes)
    assert len(audio) > 1000, f"Expected >1000 bytes from fallback, got {len(audio)}"


@pytest.mark.anyio
async def test_synthesize_for_call_valid_url():
    """Verify synthesize_for_call returns a valid URL using fallback and does not report false success on empty text."""
    agent = AIAgent(id="test-agent-alex", name="Alex")

    # 1. Valid speech turn
    url = await synthesize_for_call("Welcome to CallZenza voice testing.", agent, lang="en", is_local_test=True)
    assert url is not None
    assert url.startswith("http")
    assert "/static/audio/" in url

    # 2. Empty or whitespace text must return None (never report false success)
    empty_url = await synthesize_for_call("   ", agent, lang="en", is_local_test=True)
    assert empty_url is None
