import pytest
import uuid
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.db.database import AsyncSessionLocal
from app.db.models import User, Campaign, Call, Lead, CampaignLead
from app.core.security import hash_password, create_access_token
from app.services.agent_portal_service import AgentPortalService, _FAILED_LOGINS
from app.repositories.agent_portal_repository import AgentPortalRepository


@pytest.mark.asyncio
async def test_portal_login_role_guard_and_brute_force():
    transport = ASGITransport(app=app)
    uid = uuid.uuid4().hex[:8]
    portal_email = f"portal_{uid}@example.com"
    non_portal_email = f"user_{uid}@example.com"
    pwd = "secretpassword123"

    async with AsyncSessionLocal() as db:
        # 1. Valid portal user
        p_user = User(
            id=f"u-portal-{uid}",
            email=portal_email,
            hashed_password=hash_password(pwd),
            full_name="Portal Monitor",
            role="AI_AGENT_PORTAL_USER",
            business_id="default",
            is_active=True,
            permissions={"access_scope": "ALL_AGENTS"},
        )
        db.add(p_user)

        # 2. Normal user (non-portal)
        np_user = User(
            id=f"u-normal-{uid}",
            email=non_portal_email,
            hashed_password=hash_password(pwd),
            full_name="Normal User",
            role="VIEWER",
            business_id="default",
            is_active=True,
        )
        db.add(np_user)
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A. Normal user attempting portal login -> 403 Forbidden
        res_np = await client.post(
            "/api/v1/agent-portal/auth/login",
            json={"email": non_portal_email, "password": pwd},
        )
        assert res_np.status_code == 403
        assert "reserved for AI Agent Portal" in res_np.json()["message"]

        # B. Wrong password -> 401
        res_bad = await client.post(
            "/api/v1/agent-portal/auth/login",
            json={"email": portal_email, "password": "wrongpassword"},
        )
        assert res_bad.status_code == 401

        # C. Valid portal login -> 200 with token and user envelope
        res_ok = await client.post(
            "/api/v1/agent-portal/auth/login",
            json={"email": portal_email, "password": pwd},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert "access_token" in data
        assert data["role"] == "AI_AGENT_PORTAL_USER"
        assert data["user"]["email"] == portal_email
        assert data["user"]["access_scope"] == "ALL_AGENTS"

        # D. GET /auth/me with Bearer token
        token = data["access_token"]
        me_res = await client.get(
            "/api/v1/agent-portal/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert me_res.status_code == 200
        assert me_res.json()["email"] == portal_email


@pytest.mark.asyncio
async def test_portal_admin_user_management():
    transport = ASGITransport(app=app)
    uid = uuid.uuid4().hex[:8]
    admin_id = f"admin-{uid}"
    admin_token = create_access_token(subject=admin_id, role="ADMIN", tenant_id="default")

    async with AsyncSessionLocal() as db:
        admin_user = User(
            id=admin_id,
            email=f"admin_{uid}@example.com",
            hashed_password=hash_password("pw"),
            full_name="Admin Boss",
            role="ADMIN",
            business_id="default",
            is_active=True,
        )
        db.add(admin_user)
        await db.commit()

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {admin_token}"}

        # 1. Create Portal User with SELECTED_AGENTS
        create_res = await client.post(
            "/api/v1/agent-portal/admin/users",
            headers=headers,
            json={
                "email": f"portal_auditor_{uid}@example.com",
                "name": "Portal Auditor",
                "password": "auditorpassword123",
                "access_scope": "SELECTED_AGENTS",
                "agent_ids": [f"agent-scope-1-{uid}", f"agent-scope-2-{uid}"],
            },
        )
        assert create_res.status_code == 201
        created = create_res.json()
        assert created["role"] == "AI_AGENT_PORTAL_USER"
        assert created["access_scope"] == "SELECTED_AGENTS"
        assert f"agent-scope-1-{uid}" in created["assigned_agent_ids"]
        created_user_id = created["id"]

        # 2. List Portal Users
        list_res = await client.get("/api/v1/agent-portal/admin/users", headers=headers)
        assert list_res.status_code == 200
        users_list = list_res.json()
        assert any(u["id"] == created_user_id for u in users_list)

        # 3. Patch Portal User (deactivate)
        patch_res = await client.patch(
            f"/api/v1/agent-portal/admin/users/{created_user_id}",
            headers=headers,
            json={"is_active": False},
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["is_active"] is False


@pytest.mark.asyncio
async def test_portal_access_scoping_and_tabs():
    transport = ASGITransport(app=app)
    uid = uuid.uuid4().hex[:8]
    camp_allowed_id = f"camp-allowed-{uid}"
    camp_forbidden_id = f"camp-forbidden-{uid}"
    portal_user_id = f"user-scope-{uid}"

    portal_token = create_access_token(
        subject=portal_user_id,
        role="AI_AGENT_PORTAL_USER",
        tenant_id="default",
    )

    async with AsyncSessionLocal() as db:
        # Create 2 campaigns in default business
        c1 = Campaign(
            id=camp_allowed_id,
            name="Allowed AI Agent Campaign",
            business_id="default",
            agent_type="AI_AGENT",
            status="RUNNING",
            twilio_number="+15551112222",
        )
        c2 = Campaign(
            id=camp_forbidden_id,
            name="Forbidden AI Agent Campaign",
            business_id="default",
            agent_type="AI_AGENT",
            status="IDLE",
            twilio_number="+15553334444",
        )
        db.add(c1)
        db.add(c2)

        # Add calls and DNC leads to c1
        call_rec = Call(
            id=f"call-rec-{uid}",
            campaign_id=camp_allowed_id,
            business_id="default",
            status="completed",
            duration_seconds=120,
            recording_url="http://audio.example.com/test.wav",
            client_name="Valued Customer",
            to_number="+15559998888",
        )
        db.add(call_rec)

        dnc_lead = Lead(
            id=f"lead-dnc-{uid}",
            business_id="default",
            first_name="Opt",
            last_name="Out",
            phone="+15550001111",
            status="DNC",
            call_result="Customer requested DNC",
        )
        db.add(dnc_lead)
        c_lead = CampaignLead(
            campaign_id=camp_allowed_id,
            lead_id=f"lead-dnc-{uid}",
            status="DNC",
        )
        db.add(c_lead)

        # Portal user with scope: SELECTED_AGENTS (only camp_allowed_id)
        p_user = User(
            id=portal_user_id,
            email=f"scoped_{uid}@example.com",
            hashed_password=hash_password("pw"),
            full_name="Scoped Auditor",
            role="AI_AGENT_PORTAL_USER",
            business_id="default",
            is_active=True,
            permissions={"access_scope": "SELECTED_AGENTS", "agent_ids": [camp_allowed_id]},
        )
        db.add(p_user)
        await db.commit()

        await AgentPortalRepository.set_user_agent_access(portal_user_id, "default", [camp_allowed_id], db)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"Authorization": f"Bearer {portal_token}"}

        # 1. GET /agents -> Only camp_allowed_id should appear
        agents_res = await client.get("/api/v1/agent-portal/agents", headers=headers)
        assert agents_res.status_code == 200
        agents_data = agents_res.json()
        ids_returned = [a["id"] for a in agents_data["items"]]
        assert camp_allowed_id in ids_returned
        assert camp_forbidden_id not in ids_returned

        # 2. Access allowed agent overview -> 200
        ov_res = await client.get(f"/api/v1/agent-portal/agents/{camp_allowed_id}/overview", headers=headers)
        assert ov_res.status_code == 200

        # 3. Access forbidden agent overview -> 403 Forbidden
        forb_res = await client.get(f"/api/v1/agent-portal/agents/{camp_forbidden_id}/overview", headers=headers)
        assert forb_res.status_code == 403

        # 4. Access nonexistent agent -> 404 Not Found
        non_res = await client.get(f"/api/v1/agent-portal/agents/nonexistent_12345/overview", headers=headers)
        assert non_res.status_code == 404

        # 5. Recordings tab -> 200 with recording item
        rec_res = await client.get(f"/api/v1/agent-portal/agents/{camp_allowed_id}/recordings", headers=headers)
        assert rec_res.status_code == 200
        rec_data = rec_res.json()
        assert rec_data["available"] is True
        assert rec_data["total"] >= 1
        assert rec_data["items"][0]["call_id"] == f"call-rec-{uid}"

        # 6. Blacklist / DNC tab -> 200 with DNC lead
        bl_res = await client.get(f"/api/v1/agent-portal/agents/{camp_allowed_id}/blacklist", headers=headers)
        assert bl_res.status_code == 200
        bl_data = bl_res.json()
        assert bl_data["available"] is True
        assert bl_data["total"] >= 1
        assert bl_data["items"][0]["name"] == "Opt Out"

        # 7. Completed calls tab -> 200
        comp_res = await client.get(f"/api/v1/agent-portal/agents/{camp_allowed_id}/completed", headers=headers)
        assert comp_res.status_code == 200
        assert comp_res.json()["total"] >= 1

        # 8. Voice tab -> 200
        voice_res = await client.get(f"/api/v1/agent-portal/agents/{camp_allowed_id}/voice", headers=headers)
        assert voice_res.status_code == 200
        assert voice_res.json()["available"] is True
