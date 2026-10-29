import uuid
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User, Lead, Campaign, Call, ClientConfig, ParkedCall
from app.core.security import create_access_token, hash_password


@pytest_asyncio.fixture
async def setup_test_data():
    async with AsyncSessionLocal() as db:
        biz = Business(name="Dashboard Test Business", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        await db.flush()

        user = User(
            email=f"agent_{uuid.uuid4().hex[:6]}@example.com",
            hashed_password=hash_password("pass123"),
            full_name="Agent Smith",
            role="VOICE_AGENT",
            business_id=biz.id,
            status="AVAILABLE",
        )
        db.add(user)
        await db.flush()

        lead = Lead(
            business_id=biz.id,
            first_name="Jane",
            last_name="Doe",
            phone="+15550192834",
            email="jane@example.com",
            company="Acme Corp",
            requirement="Solar Installation Inquiry",
        )
        db.add(lead)
        await db.flush()

        campaign = Campaign(
            business_id=biz.id,
            name="Solar Campaign Test",
            client_name="SunPower Client",
            client_phone="+15550193333",
        )
        db.add(campaign)
        await db.flush()

        call = Call(
            business_id=biz.id,
            lead_id=lead.id,
            agent_id=user.id,
            direction="OUTBOUND",
            from_number="+15550192834",
            to_number="+15550192834",
            status="COMPLETED",
            duration_seconds=180,
            call_result="Sale",
        )
        db.add(call)
        await db.commit()

        token = create_access_token(user.id, user.role)
        headers = {"Authorization": f"Bearer {token}"}

        yield {
            "biz_id": biz.id,
            "user": user,
            "lead": lead,
            "campaign": campaign,
            "call": call,
            "headers": headers,
        }


@pytest.mark.asyncio
async def test_agent_dashboard_stats(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/voice/dashboard/stats", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_calls" in data
        assert "total_time_seconds" in data
        assert "calls_in_queue" in data
        assert "calls_on_hold" in data
        assert "agent_status" in data


@pytest.mark.asyncio
async def test_agent_status_update(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/api/voice/agent/status", json={"status": "PAUSED"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "PAUSED"

        resp2 = await client.post("/api/voice/agent/status", json={"status": "AVAILABLE"}, headers=headers)
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "AVAILABLE"


@pytest.mark.asyncio
async def test_my_calls_history(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/voice/calls/my-calls", headers=headers)
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        assert len(resp.json()) >= 1


@pytest.mark.asyncio
async def test_total_time_breakdown(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/voice/dashboard/total-time", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "total_duration_seconds" in data
        assert "formatted_total_time" in data
        assert "individual_calls" in data
        assert "date_wise_history" in data


@pytest.mark.asyncio
async def test_queue_and_on_hold_endpoints(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_q = await client.get("/api/voice/queue", headers=headers)
        assert resp_q.status_code == 200
        assert isinstance(resp_q.json(), list)

        resp_h = await client.get("/api/voice/on-hold", headers=headers)
        assert resp_h.status_code == 200
        assert isinstance(resp_h.json(), list)


@pytest.mark.asyncio
async def test_client_config_crud(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Get client configs
        resp = await client.get("/api/voice/client-configs", headers=headers)
        assert resp.status_code == 200

        # Save D1 client config
        payload = {
            "slot": "D1",
            "client_name": "Test Solar Client",
            "client_phone": "+15550193333"
        }
        resp_save = await client.post("/api/voice/client-configs", json=payload, headers=headers)
        assert resp_save.status_code == 200
        data = resp_save.json()
        assert data["slot"] == "D1"
        assert data["client_name"] == "Test Solar Client"
        assert data["client_phone"] == "+15550193333"

        # Delete slot D1
        resp_del = await client.delete("/api/voice/client-configs/D1", headers=headers)
        assert resp_del.status_code == 200


@pytest.mark.asyncio
async def test_park_and_resume_call(setup_test_data):
    headers = setup_test_data["headers"]
    call_id = setup_test_data["call"].id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp_park = await client.post(f"/api/voice/calls/{call_id}/park", json={"slot": "D1", "hold_reason": "Testing Park"}, headers=headers)
        assert resp_park.status_code == 200
        assert resp_park.json()["status"] == "PARKED"

        resp_resume = await client.post(f"/api/voice/calls/{call_id}/resume-park", headers=headers)
        assert resp_resume.status_code == 200
        assert resp_resume.json()["status"] == "HUMAN_HANDLING"


@pytest.mark.asyncio
async def test_connect_client_3way(setup_test_data):
    headers = setup_test_data["headers"]
    call_id = setup_test_data["call"].id
    payload = {"slot": "D1", "client_phone": "+15550193333", "client_name": "Client Representative"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post(f"/api/voice/calls/{call_id}/connect-client", json=payload, headers=headers)
        assert resp.status_code == 200
        assert "status" in resp.json()


@pytest.mark.asyncio
async def test_transfer_recording_and_end_call(setup_test_data):
    headers = setup_test_data["headers"]
    call_id = setup_test_data["call"].id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Transfer
        resp_tr = await client.post(f"/api/voice/calls/{call_id}/transfer", json={"destination_number": "+15550198888"}, headers=headers)
        assert resp_tr.status_code == 200

        # Start Recording
        resp_rec = await client.post(f"/api/voice/calls/{call_id}/recording/start", headers=headers)
        assert resp_rec.status_code == 200
        assert "record_id" in resp_rec.json()

        # Stop Recording
        resp_stop = await client.post(f"/api/voice/calls/{call_id}/recording/stop", headers=headers)
        assert resp_stop.status_code == 200

        # End Call
        resp_end = await client.post(f"/api/voice/calls/{call_id}/end", json={"duration_seconds": 120, "outcome": "Sale"}, headers=headers)
        assert resp_end.status_code == 200
        assert resp_end.json()["status"] == "COMPLETED"


@pytest.mark.asyncio
async def test_dial_next_lead(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/voice/leads/next", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "lead" in data
        assert "campaign" in data


@pytest.mark.asyncio
async def test_get_lead_list_customers_no_params_returns_empty(setup_test_data):
    headers = setup_test_data["headers"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/voice/leads/list-customers", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data == []


@pytest.mark.asyncio
async def test_get_lead_list_customers_with_group_id(setup_test_data):
    headers = setup_test_data["headers"]
    biz_id = setup_test_data["biz_id"]

    from app.db.models import LeadGroup
    async with AsyncSessionLocal() as db:
        lg = LeadGroup(business_id=biz_id, filename="uploaded_test.csv", lead_count=1)
        db.add(lg)
        await db.flush()

        grp_lead = Lead(
            business_id=biz_id,
            group_id=lg.id,
            first_name="GroupMember",
            last_name="One",
            phone="+19998887777",
            email="grp1@example.com",
            company="Group Company",
        )
        db.add(grp_lead)
        await db.commit()
        lg_id = lg.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/api/voice/leads/list-customers?lead_group_id={lg_id}", headers=headers)
        assert resp.status_code == 200
        customers = resp.json()
        assert len(customers) == 1
        assert customers[0]["first_name"] == "GroupMember"
        assert customers[0]["phone"] == "+19998887777"

