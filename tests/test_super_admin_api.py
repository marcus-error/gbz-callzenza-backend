import asyncio
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.db.database import AsyncSessionLocal, init_db
from app.db.models import User, Business
from app.core.security import create_access_token


async def run_super_admin_tests():
    print("\n=======================================================")
    print("STARTING SUPER ADMIN API ROUTE VERIFICATION")
    print("=======================================================\n")

    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:

        # Step 1: Locate or verify super admin account
        print("[TEST 1] Verifying SUPER_ADMIN account and tokens...")
        async with AsyncSessionLocal() as session:
            admin = (await session.scalars(select(User).where(User.email == "admin@callzenza.com"))).first()
            assert admin is not None, "admin@callzenza.com must exist!"
            assert admin.role == "SUPER_ADMIN", f"Role is {admin.role}, expected SUPER_ADMIN"
            admin_id = admin.id
            admin_biz = admin.business_id

            # Also find or create a normal user for RBAC negative test
            normal_user = (await session.scalars(select(User).where(User.role == "VOICE_AGENT"))).first()
            normal_user_id = normal_user.id if normal_user else "normal-user-id"

        admin_token = create_access_token(subject=admin_id, role="SUPER_ADMIN", tenant_id=admin_biz)
        user_token = create_access_token(subject=normal_user_id, role="VOICE_AGENT", tenant_id=admin_biz)

        admin_headers = {"Authorization": f"Bearer {admin_token}"}
        user_headers = {"Authorization": f"Bearer {user_token}"}
        print("  ✓ Admin token generated.")

        # Step 2: RBAC security test
        print("[TEST 2] Verifying RBAC protection for Super Admin endpoints...")
        rbac_res = await client.get("/api/super-admin/dashboard", headers=user_headers)
        assert rbac_res.status_code in (401, 403), f"Expected 401/403 for non-super-admin, got {rbac_res.status_code}"
        print("  ✓ Non-super-admin correctly blocked.")

        # Step 3: GET /api/super-admin/dashboard
        print("[TEST 3] Testing GET /api/super-admin/dashboard...")
        dash_res = await client.get("/api/super-admin/dashboard", headers=admin_headers)
        assert dash_res.status_code == 200, f"Dashboard failed: {dash_res.text}"
        dash_data = dash_res.json()
        assert "total_tenants" in dash_data
        assert "total_calls" in dash_data
        assert "server_health" in dash_data
        print(f"  ✓ Dashboard metrics loaded: {dash_data['total_tenants']} tenants, {dash_data['total_calls']} calls, MRR: {dash_data.get('mrr_formatted')}")

        # Step 4: GET /api/super-admin/tenants
        print("[TEST 4] Testing GET /api/super-admin/tenants...")
        tenants_res = await client.get("/api/super-admin/tenants?page=1&limit=10", headers=admin_headers)
        assert tenants_res.status_code == 200, f"Tenants list failed: {tenants_res.text}"
        tenants_data = tenants_res.json()
        assert "tenants" in tenants_data
        assert tenants_data["total"] >= 0
        print(f"  ✓ Tenants list loaded: {len(tenants_data['tenants'])} returned of {tenants_data['total']} total.")

        # Step 5: GET /api/super-admin/server-status
        print("[TEST 5] Testing GET /api/super-admin/server-status...")
        status_res = await client.get("/api/super-admin/server-status", headers=admin_headers)
        assert status_res.status_code == 200, f"Server status failed: {status_res.text}"
        status_data = status_res.json()
        assert "cpu_usage_percent" in status_data
        assert "memory_usage_percent" in status_data
        print(f"  ✓ Server status: CPU {status_data['cpu_usage_percent']}%, RAM {status_data['memory_usage_percent']}%")

        # Step 6: GET /api/super-admin/storage
        print("[TEST 6] Testing GET /api/super-admin/storage...")
        storage_res = await client.get("/api/super-admin/storage", headers=admin_headers)
        assert storage_res.status_code == 200, f"Storage metrics failed: {storage_res.text}"
        storage_data = storage_res.json()
        assert "total_storage_used_formatted" in storage_data
        print(f"  ✓ Storage status: Total used {storage_data['total_storage_used_formatted']}")

        # Step 7: GET /api/super-admin/subscriptions
        print("[TEST 7] Testing GET /api/super-admin/subscriptions...")
        plans_res = await client.get("/api/super-admin/subscriptions", headers=admin_headers)
        assert plans_res.status_code == 200, f"Subscriptions failed: {plans_res.text}"
        plans_data = plans_res.json()
        assert isinstance(plans_data, list)
        print(f"  ✓ Subscriptions plans returned: {len(plans_data)} plans available.")

        # Step 8: GET /api/super-admin/analytics for each time range
        print("[TEST 8] Testing GET /api/super-admin/analytics across multiple time ranges...")
        for r in ["today", "7d", "30d", "month", "all"]:
            analytics_res = await client.get(f"/api/super-admin/analytics?time_range={r}", headers=admin_headers)
            assert analytics_res.status_code == 200, f"Analytics for {r} failed: {analytics_res.text}"
            adata = analytics_res.json()
            assert "total_calls" in adata
            assert "total_call_duration_seconds" in adata
            assert "total_call_duration_minutes" in adata
            assert "tenant_growth" in adata
            assert "call_volume_trends" in adata
            assert "subscription_distribution" in adata
        print("  ✓ Analytics passed for today, 7d, 30d, month, and all time ranges!")

        # Step 9: Tenant detail, analytics, billing for an existing business
        print("[TEST 9] Testing tenant detail, analytics, and billing endpoints...")
        async with AsyncSessionLocal() as session:
            test_biz = (await session.scalars(select(Business).where(Business.id != admin_biz))).first()
            if not test_biz:
                test_biz = (await session.scalars(select(Business))).first()

        if test_biz:
            b_id = test_biz.id
            tdetail = await client.get(f"/api/super-admin/tenants/{b_id}", headers=admin_headers)
            assert tdetail.status_code == 200, f"Tenant detail failed: {tdetail.text}"
            print(f"  ✓ Tenant detail for '{test_biz.name}' retrieved.")

            tanalytics = await client.get(f"/api/super-admin/tenants/{b_id}/analytics?time_range=30d", headers=admin_headers)
            assert tanalytics.status_code == 200, f"Tenant analytics failed: {tanalytics.text}"
            print(f"  ✓ Tenant analytics for '{test_biz.name}' retrieved.")

            tbilling = await client.get(f"/api/super-admin/tenants/{b_id}/billing", headers=admin_headers)
            assert tbilling.status_code == 200, f"Tenant billing failed: {tbilling.text}"
            print(f"  ✓ Tenant billing for '{test_biz.name}' retrieved.")

    print("\n=======================================================")
    print("ALL SUPER ADMIN API ROUTES VERIFIED & WORKING PERFECTLY!")
    print("=======================================================\n")


if __name__ == "__main__":
    asyncio.run(run_super_admin_tests())
