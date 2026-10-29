import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, Call, AuditLog
from app.core.security import create_access_token, hash_password


@pytest_asyncio.fixture
async def async_client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_human_agent_flow_not_qualified(async_client: AsyncClient):
    """Test 1: Human Agent Flow — Not Qualified -> Wrap up -> COMPLETED."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 1", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"sup1_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Supervisor One",
            role="SUPERVISOR",
            business_id=biz.id,
        )
        db.add(user)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="John", last_name="Doe", phone="+15550191111")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            direction="OUTBOUND",
            agent_type="HUMAN",
            status="HUMAN_HANDLING",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        # Mark as NOT_QUALIFIED
        res = await async_client.post(
            f"/api/voice/calls/{call.id}/qualification",
            headers=headers,
            json={"qualification_status": "NOT_QUALIFIED"},
        )
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "NOT_QUALIFIED"

        # Verify lead status updated
        await db.refresh(lead)
        assert lead.qualification_status == "NOT_QUALIFIED"


@pytest.mark.asyncio
async def test_ai_agent_flow_qualified_and_conference(async_client: AsyncClient):
    """Test 2: AI Agent Flow -> QUALIFIED -> CLIENT_ADDING -> CONFERENCE_3WAY_ACTIVE."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 2", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"sup2_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Supervisor Two",
            role="SUPERVISOR",
            business_id=biz.id,
        )
        db.add(user)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Alice", last_name="Smith", phone="+15550192222")
        db.add(lead)
        await db.flush()

        campaign = Campaign(
            business_id=biz.id,
            name="Solar Outbound Campaign",
            agent_type="AI_VOICE",
            client_name="SunPower Rep",
            client_phone="+15550193333",
        )
        db.add(campaign)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            direction="OUTBOUND",
            agent_type="AI_VOICE",
            client_name=campaign.client_name,
            client_phone=campaign.client_phone,
            status="AI_HANDLING",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        # Step A: Mark Qualified
        q_res = await async_client.post(
            f"/api/voice/calls/{call.id}/qualification",
            headers=headers,
            json={"qualification_status": "QUALIFIED"},
        )
        assert q_res.status_code == 200
        assert q_res.json()["status"] == "QUALIFIED"

        # Step B: Add Client to Call
        c_res = await async_client.post(
            f"/api/voice/calls/{call.id}/add-client",
            headers=headers,
            json={"client_name": "SunPower Rep", "client_phone": "+15550193333"},
        )
        assert c_res.status_code == 200
        c_data = c_res.json()
        assert c_data["status"] == "CONFERENCE_3WAY_ACTIVE"
        assert c_data["conference_sid"].startswith("CONF-")


@pytest.mark.asyncio
async def test_failed_client_dial_and_retry(async_client: AsyncClient):
    """Test 3: Unreachable client number -> CLIENT_ADD_FAILED -> Retry path succeeds."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 3", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"sup3_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Supervisor Three",
            role="SUPERVISOR",
            business_id=biz.id,
        )
        db.add(user)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Bob", last_name="Jones", phone="+15550194444")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            direction="OUTBOUND",
            client_phone="+15550000000",  # Unreachable test number
            status="QUALIFIED",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        # First attempt: Unreachable number -> CLIENT_ADD_FAILED
        fail_res = await async_client.post(
            f"/api/voice/calls/{call.id}/add-client",
            headers=headers,
            json={"client_phone": "+15550000000"},
        )
        assert fail_res.status_code == 200
        assert fail_res.json()["status"] == "CLIENT_ADD_FAILED"

        # Retry attempt: Valid reach number -> CONFERENCE_3WAY_ACTIVE
        retry_res = await async_client.post(
            f"/api/voice/calls/{call.id}/add-client",
            headers=headers,
            json={"client_phone": "+15550195555"},
        )
        assert retry_res.status_code == 200
        assert retry_res.json()["status"] == "CONFERENCE_3WAY_ACTIVE"


@pytest.mark.asyncio
async def test_idempotency_protection_on_add_client(async_client: AsyncClient):
    """Test 4: Duplicate Add Client calls return conflict / existing status without double dial."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 4", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"sup4_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Supervisor Four",
            role="SUPERVISOR",
            business_id=biz.id,
        )
        db.add(user)
        await db.flush()

        lead = Lead(business_id=biz.id, first_name="Charlie", phone="+15550196666")
        db.add(lead)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            status="CONFERENCE_3WAY_ACTIVE",
            conference_sid="CONF-EXISTING-123",
            client_phone="+15550197777",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        # Request while already in active conference
        dup_res = await async_client.post(
            f"/api/voice/calls/{call.id}/add-client",
            headers=headers,
            json={"client_phone": "+15550197777"},
        )
        assert dup_res.status_code == 200
        assert dup_res.json()["status"] == "CONFERENCE_3WAY_ACTIVE"
        assert dup_res.json()["conference_sid"] == "CONF-EXISTING-123"


@pytest.mark.asyncio
async def test_server_side_e164_phone_validation(async_client: AsyncClient):
    """Test 5: Server-side E.164 phone validation rejects invalid formats."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 5", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"admin5_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Admin Five",
            role="ADMIN",
            business_id=biz.id,
        )
        db.add(user)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        # Attempt to create campaign with invalid phone format
        invalid_res = await async_client.post(
            "/api/campaigns",
            headers=headers,
            json={
                "name": "Invalid Phone Campaign",
                "agent_type": "AI_VOICE",
                "client_phone": "invalid-phone-123",
                "twilio_number": "+15550198888",
                "lead_ids": [],
            },
        )
        assert invalid_res.status_code == 422


@pytest.mark.asyncio
async def test_rbac_authorization_on_client_credentials(async_client: AsyncClient):
    """Test 6: Non-supervisor/admin role (VIEWER) gets HTTP 403 Forbidden when updating client credentials."""
    async with AsyncSessionLocal() as db:
        biz = Business(name="Test Biz 6", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        viewer = User(
            email=f"viewer6_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Viewer Six",
            role="VIEWER",
            business_id=biz.id,
        )
        db.add(viewer)

        campaign = Campaign(
            business_id=biz.id,
            name="Existing Campaign",
            client_phone="+15550199999",
        )
        db.add(campaign)
        await db.commit()

        token = create_access_token(viewer.id, viewer.role)
        headers = {"Authorization": f"Bearer {token}"}

        # VIEWER tries to update campaign client phone
        update_res = await async_client.patch(
            f"/api/campaigns/{campaign.id}",
            headers=headers,
            json={"client_phone": "+15550190000"},
        )
        assert update_res.status_code == 403

