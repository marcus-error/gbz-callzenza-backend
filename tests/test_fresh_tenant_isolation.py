import asyncio
import os
import sys
import uuid
import httpx
import pytest
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

BASE_URL = "http://127.0.0.1:8000"

async def run_isolation_tests():
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        print("=== STEP 1: Authenticate Superadmin ===")
        # Login with superadmin credentials
        sa_resp = await client.post("/api/super-admin/auth/login", json={
            "email": "admin@callzenza.com",
            "password": "admin123"
        })
        assert sa_resp.status_code == 200, f"Superadmin login failed: {sa_resp.text}"
        sa_data = sa_resp.json()
        sa_token = sa_data["access_token"]
        sa_headers = {"Authorization": f"Bearer {sa_token}"}
        print("Superadmin authenticated successfully.")

        print("\n=== STEP 2: Verify Superadmin Cross-Tenant Access ===")
        tenants_res = await client.get("/api/super-admin/tenants", headers=sa_headers)
        assert tenants_res.status_code == 200, f"Failed listing tenants: {tenants_res.text}"
        tenants = tenants_res.json().get("tenants", [])
        print(f"Total existing tenants visible to Superadmin: {len(tenants)}")
        assert len(tenants) > 0, "Expected at least 1 existing tenant"

        print("\n=== STEP 3: Create Brand New Tenant B via Superadmin ===")
        unique_id = str(uuid.uuid4())[:8]
        tenant_b_slug = f"tenant-test-{unique_id}"
        tenant_b_email = f"admin@{tenant_b_slug}.com"
        tenant_b_pwd = "SecureTenantPass123!"

        create_res = await client.post("/api/super-admin/tenants", headers=sa_headers, json={
            "name": f"Fresh Isolated Tenant {unique_id}",
            "slug": tenant_b_slug,
            "email": tenant_b_email,
            "password": tenant_b_pwd,
            "current_plan": "Enterprise Pro",
            "industry": "Real Estate",
            "cluster_id": "default",
            "isolation_strategy": "SHARED_DATABASE"
        })
        assert create_res.status_code in (200, 201), f"Failed creating tenant B: {create_res.text}"
        tenant_b_info = create_res.json()
        tenant_b_id = tenant_b_info["id"]
        print(f"Tenant B created successfully! ID: {tenant_b_id}, Email: {tenant_b_email}")

        print("\n=== STEP 4: Authenticate as Tenant B Admin ===")
        login_res = await client.post("/api/auth/login", json={
            "email": tenant_b_email,
            "password": tenant_b_pwd
        })
        assert login_res.status_code == 200, f"Tenant B login failed: {login_res.text}"
        b_auth = login_res.json()
        b_token = b_auth.get("access_token") or b_auth.get("token")
        b_headers = {"Authorization": f"Bearer {b_token}"}
        print("Tenant B logged in successfully with isolated token.")

        print("\n=== STEP 5: Verify Tenant B Workspace is Completely Fresh (0 records) ===")

        # 5a. Call History
        b_calls_res = await client.get("/api/voice/calls/my-calls", headers=b_headers)
        assert b_calls_res.status_code == 200, f"Call history error: {b_calls_res.text}"
        b_calls = b_calls_res.json()
        print(f"Tenant B Call History count: {len(b_calls)} (Expected: 0)")
        assert len(b_calls) == 0, f"Call history leaked! Found {len(b_calls)} calls"

        # 5b. Reports - Overall Calls
        b_rep_calls = await client.get("/api/reports/calls", headers=b_headers)
        assert b_rep_calls.status_code == 200, f"Reports calls error: {b_rep_calls.text}"
        b_rep_calls_data = b_rep_calls.json()
        calls_list = b_rep_calls_data if isinstance(b_rep_calls_data, list) else b_rep_calls_data.get("calls", [])
        print(f"Tenant B Reports Call list count: {len(calls_list)} (Expected: 0)")
        assert len(calls_list) == 0, f"Reports call list leaked! Found {len(calls_list)} calls"

        # 5c. Campaigns
        b_camps_res = await client.get("/api/campaigns", headers=b_headers)
        assert b_camps_res.status_code == 200, f"Campaigns error: {b_camps_res.text}"
        b_camps = b_camps_res.json()
        print(f"Tenant B Campaigns count: {len(b_camps)} (Expected: 0)")
        assert len(b_camps) == 0, f"Campaigns leaked! Found {len(b_camps)} campaigns"

        # 5d. Leads
        b_leads_res = await client.get("/api/leads", headers=b_headers)
        assert b_leads_res.status_code == 200, f"Leads error: {b_leads_res.text}"
        b_leads = b_leads_res.json()
        print(f"Tenant B Leads count: {len(b_leads)} (Expected: 0)")
        assert len(b_leads) == 0, f"Leads leaked! Found {len(b_leads)} leads"

        # 5e. Lead Groups
        b_groups_res = await client.get("/api/lead-groups", headers=b_headers)
        assert b_groups_res.status_code == 200, f"Lead groups error: {b_groups_res.text}"
        b_groups = b_groups_res.json()
        print(f"Tenant B Lead Groups count: {len(b_groups)} (Expected: 0)")
        assert len(b_groups) == 0, f"Lead groups leaked! Found {len(b_groups)} groups"

        # 5f. Call Recordings
        b_recs_res = await client.get("/api/reports/call-recordings", headers=b_headers)
        assert b_recs_res.status_code == 200, f"Recordings error: {b_recs_res.text}"
        b_recs = b_recs_res.json()
        recs_list = b_recs.get("recordings", []) if isinstance(b_recs, dict) else b_recs
        print(f"Tenant B Call Recordings count: {len(recs_list)} (Expected: 0)")
        assert len(recs_list) == 0, f"Recordings leaked! Found {len(recs_list)} recordings"

        # 5g. Inbound Stats
        b_inb_stats = await client.get("/api/inbound/stats/summary", headers=b_headers)
        assert b_inb_stats.status_code == 200, f"Inbound stats error: {b_inb_stats.text}"
        inb_data = b_inb_stats.json()
        print(f"Tenant B Inbound Stats: {inb_data}")
        assert inb_data.get("total_numbers") == 0, "Inbound numbers leaked!"
        assert inb_data.get("calls_today") == 0, "Inbound calls today leaked!"
        assert inb_data.get("total_inbound_calls") == 0, "Total inbound calls leaked!"

        # 5h. Dashboard Metrics
        b_dash = await client.get("/api/voice/dashboard/stats", headers=b_headers)
        assert b_dash.status_code == 200, f"Dashboard stats error: {b_dash.text}"
        dash_data = b_dash.json()
        print(f"Tenant B Dashboard Stats - Total Calls: {dash_data.get('total_calls')}, Today Calls: {dash_data.get('today_calls')}")
        assert dash_data.get("total_calls") == 0, "Dashboard total_calls count is not 0!"
        assert dash_data.get("today_calls") == 0, "Dashboard today_calls is not 0!"
        assert dash_data.get("calls_in_queue") == 0, "Dashboard calls_in_queue is not 0!"
        assert dash_data.get("calls_on_hold") == 0, "Dashboard calls_on_hold is not 0!"

        # 5h-2. Daily Report Metrics
        b_daily = await client.get("/api/voice/dashboard/daily-report", headers=b_headers)
        assert b_daily.status_code == 200, f"Daily report error: {b_daily.text}"
        daily_data = b_daily.json()
        daily_summary = daily_data.get("summary", {})
        print(f"Tenant B Daily Report - Total Calls: {daily_summary.get('total_calls')}, Completed: {daily_summary.get('completed_calls')}")
        assert daily_summary.get("total_calls") == 0, "Daily report total_calls is not 0!"
        assert daily_summary.get("completed_calls") == 0, "Daily report completed_calls is not 0!"

        # 5i. Voice Agents
        b_va = await client.get("/api/voice-agents", headers=b_headers)
        assert b_va.status_code == 200, f"Voice agents error: {b_va.text}"
        b_va_list = b_va.json()
        print(f"Tenant B Voice Agents count: {len(b_va_list)} (Expected: 0)")
        assert len(b_va_list) == 0, f"Voice agents leaked! Found {len(b_va_list)} voice agents"

        print("\n=== STEP 6: Test Cross-Tenant Access Prevention ===")
        # Get an existing campaign belonging to Tenant A (from DB or Superadmin)
        from app.db.database import AsyncSessionLocal
        from app.db.models import Campaign as DBCampaign, Lead as DBLead, Call as DBCall
        from sqlalchemy import select

        async with AsyncSessionLocal() as session:
            t_a_camp = (await session.execute(
                select(DBCampaign).where(DBCampaign.business_id != tenant_b_id)
            )).scalars().first()

            t_a_lead = (await session.execute(
                select(DBLead).where(DBLead.business_id != tenant_b_id)
            )).scalars().first()

            t_a_call = (await session.execute(
                select(DBCall).where(DBCall.business_id != tenant_b_id)
            )).scalars().first()

        if t_a_camp:
            print(f"Testing unauthorized access to Tenant A's Campaign ({t_a_camp.id}) using Tenant B's token...")
            camp_attack = await client.get(f"/api/campaigns/{t_a_camp.id}", headers=b_headers)
            print(f"Status code received: {camp_attack.status_code}")
            assert camp_attack.status_code in (403, 404), f"Cross-tenant campaign access was NOT denied! Status: {camp_attack.status_code}"
            print("Access correctly DENIED (403/404)!")

        if t_a_lead:
            print(f"Testing unauthorized access to Tenant A's Lead ({t_a_lead.id}) using Tenant B's token...")
            lead_attack = await client.get(f"/api/leads/{t_a_lead.id}", headers=b_headers)
            print(f"Status code received: {lead_attack.status_code}")
            assert lead_attack.status_code in (403, 404), f"Cross-tenant lead access was NOT denied! Status: {lead_attack.status_code}"
            print("Access correctly DENIED (403/404)!")

        print("\n=== ALL 12 MULTI-TENANT ISOLATION TESTS PASSED PERFECTLY! ===")

if __name__ == "__main__":
    asyncio.run(run_isolation_tests())
