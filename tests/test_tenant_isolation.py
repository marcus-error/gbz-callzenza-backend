import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.security import create_access_token, hash_password
from app.db.database import AsyncSessionLocal
from app.db.models import User
from app.main import app
from app.models.organization import Organization
from app.models.tenant import Tenant


@pytest.mark.asyncio
async def test_multi_tenant_isolation_end_to_end():
    from sqlalchemy import select as sa_select

    async with AsyncSessionLocal() as session:
        # 1. Create two test tenants in DB (idempotent — skip if slug already exists)
        result_a = await session.execute(sa_select(Tenant).where(Tenant.slug == "alpha-corp"))
        tenant_a = result_a.scalar_one_or_none()
        if not tenant_a:
            tenant_a = Tenant(name="Tenant Alpha Corp", slug="alpha-corp", status="active")
            session.add(tenant_a)
            await session.flush()

        result_b = await session.execute(sa_select(Tenant).where(Tenant.slug == "beta-ltd"))
        tenant_b = result_b.scalar_one_or_none()
        if not tenant_b:
            tenant_b = Tenant(name="Tenant Beta Ltd", slug="beta-ltd", status="active")
            session.add(tenant_b)
            await session.flush()

        await session.commit()
        await session.refresh(tenant_a)
        await session.refresh(tenant_b)


        # 2. Create users for both tenants (idempotent — skip if email already exists)
        result_ua = await session.execute(sa_select(User).where(User.email == "admin@alpha.com"))
        user_a = result_ua.scalar_one_or_none()
        if not user_a:
            user_a = User(
                email="admin@alpha.com",
                hashed_password=hash_password("password123"),
                full_name="Alpha Admin",
                role="ADMIN",
                business_id=tenant_a.id,
                is_active=True,
            )
            session.add(user_a)
            await session.flush()

        result_ub = await session.execute(sa_select(User).where(User.email == "admin@beta.com"))
        user_b = result_ub.scalar_one_or_none()
        if not user_b:
            user_b = User(
                email="admin@beta.com",
                hashed_password=hash_password("password123"),
                full_name="Beta Admin",
                role="ADMIN",
                business_id=tenant_b.id,
                is_active=True,
            )
            session.add(user_b)
            await session.flush()

        await session.commit()
        await session.refresh(user_a)
        await session.refresh(user_b)

    # 3. Create tokens for both users
    token_a = create_access_token(subject=user_a.id, role=user_a.role, tenant_id=tenant_a.id)
    token_b = create_access_token(subject=user_b.id, role=user_b.role, tenant_id=tenant_b.id)

    headers_a = {"Authorization": f"Bearer {token_a}"}
    headers_b = {"Authorization": f"Bearer {token_b}"}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # A. Test Organization Isolation
        create_org_resp = await client.post(
            "/api/organizations",
            json={"name": "Alpha Engineering Branch", "org_type": "BRANCH"},
            headers=headers_a,
        )
        assert create_org_resp.status_code == 201
        org_a_id = create_org_resp.json()["id"]

        # Tenant A lists organizations -> should see Alpha Engineering
        list_a_resp = await client.get("/api/organizations", headers=headers_a)
        assert list_a_resp.status_code == 200
        org_ids_a = [o["id"] for o in list_a_resp.json()]
        assert org_a_id in org_ids_a

        # Tenant B lists organizations -> MUST NOT see Tenant A's organization
        list_b_resp = await client.get("/api/organizations", headers=headers_b)
        assert list_b_resp.status_code == 200
        org_ids_b = [o["id"] for o in list_b_resp.json()]
        assert org_a_id not in org_ids_b

        # Tenant B tries to fetch Tenant A's organization directly by ID -> MUST return 404
        direct_get_b = await client.get(f"/api/organizations/{org_a_id}", headers=headers_b)
        assert direct_get_b.status_code == 404

        import uuid
        unique_email = f"agent.smith.{uuid.uuid4().hex[:6]}@alpha.com"
        create_user_resp = await client.post(
            "/api/tenant/users",
            json={
                "email": unique_email,
                "password": "strongPassword123!",
                "full_name": "Agent Smith",
                "role": "VOICE_AGENT",
            },
            headers=headers_a,
        )
        assert create_user_resp.status_code == 201
        smith_id = create_user_resp.json()["id"]

        # Tenant B lists tenant users -> MUST NOT see Agent Smith
        b_users_resp = await client.get("/api/tenant/users", headers=headers_b)
        assert b_users_resp.status_code == 200
        b_user_ids = [u["id"] for u in b_users_resp.json()]
        assert smith_id not in b_user_ids

        unique_role_name = f"Alpha Custom Triage Role {uuid.uuid4().hex[:6]}"
        create_role_resp = await client.post(
            "/api/roles",
            json={"name": unique_role_name, "description": "Custom role for Alpha only"},
            headers=headers_a,
        )
        assert create_role_resp.status_code == 201
        role_a_id = create_role_resp.json()["id"]

        # Tenant B lists roles -> should NOT see Alpha custom role
        b_roles_resp = await client.get("/api/roles", headers=headers_b)
        assert b_roles_resp.status_code == 200
        b_role_ids = [r["id"] for r in b_roles_resp.json()]
        assert role_a_id not in b_role_ids

        # D. Test Branding Isolation
        update_branding_resp = await client.put(
            "/api/branding",
            json={"company_name": "Alpha Super Brand", "primary_color": "#112233"},
            headers=headers_a,
        )
        assert update_branding_resp.status_code == 200
        assert update_branding_resp.json()["company_name"] == "Alpha Super Brand"

        # Tenant B gets its branding -> should not be Alpha's branding
        b_branding_resp = await client.get("/api/branding", headers=headers_b)
        assert b_branding_resp.status_code == 200
        assert b_branding_resp.json()["company_name"] != "Alpha Super Brand"

        # E. Test Tenant Settings Isolation
        update_settings_resp = await client.put(
            "/api/tenant/settings",
            json={"timezone": "Asia/Tokyo", "currency": "JPY", "retry_limit": 5},
            headers=headers_a,
        )
        assert update_settings_resp.status_code == 200
        assert update_settings_resp.json()["timezone"] == "Asia/Tokyo"

        b_settings_resp = await client.get("/api/tenant/settings", headers=headers_b)
        assert b_settings_resp.status_code == 200
        assert b_settings_resp.json()["timezone"] != "Asia/Tokyo"

        # F. Test Dashboard Isolation
        dashboard_a = await client.get("/api/tenant/dashboard", headers=headers_a)
        assert dashboard_a.status_code == 200
        data_a = dashboard_a.json()
        assert data_a["tenant_id"] == tenant_a.id

        dashboard_b = await client.get("/api/tenant/dashboard", headers=headers_b)
        assert dashboard_b.status_code == 200
        data_b = dashboard_b.json()
        assert data_b["tenant_id"] == tenant_b.id
