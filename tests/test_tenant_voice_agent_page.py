import asyncio
import uuid
import pytest
from sqlalchemy import select
from app.db.database import AsyncSessionLocal
from app.db.models import User, Business, Expert, AIAgent
from app.api.routes.voice_agents import list_voice_agents, create_voice_agent
from app.api.routes.agents import list_agents, create_agent
from app.schemas.agent import AgentCreate

@pytest.mark.asyncio
async def test_tenant_voice_agent_and_ai_agent_visibility():
    async with AsyncSessionLocal() as db:
        run_id = str(uuid.uuid4())[:6]
        tenant_id = f"test-tenant-{run_id}"

        # 1. Create Tenant Business and Tenant Owner
        admin_id = str(uuid.uuid4())
        biz = Business(
            id=tenant_id,
            name=f"Tenant Corp {run_id}",
            slug=f"tenant-corp-{run_id}",
            owner_user_id=admin_id,
            is_active=True,
            current_plan="Pro",
        )
        db.add(biz)

        tenant_admin = User(
            id=admin_id,
            email=f"admin_{run_id}@tenantcorp.com",
            hashed_password="hash",
            full_name=f"Tenant Corp {run_id} Admin",
            role="BUSINESS_OWNER",
            business_id=tenant_id,
            is_active=True,
        )
        db.add(tenant_admin)
        await db.commit()

        # 2. Verify initially 0 Voice Agents for this tenant
        va_initial = await list_voice_agents(agent_type=None, user=tenant_admin, db=db)
        assert len(va_initial) == 0, f"Expected 0 initial voice agents, got {len(va_initial)}"

        # 3. Create a Voice Agent under this tenant
        created_va = await create_voice_agent(
            body={
                "name": f"Agent Vikram {run_id}",
                "email": f"vikram_{run_id}@tenantcorp.com",
                "phone": "2001",
                "direction": "INBOUND",
                "status": "AVAILABLE"
            },
            user=tenant_admin,
            db=db
        )
        assert created_va.name == f"Agent Vikram {run_id}"
        assert created_va.business_id == tenant_id

        # 4. Verify Voice Agent is now visible in the agent page listing for this tenant
        va_list = await list_voice_agents(agent_type=None, user=tenant_admin, db=db)
        va_emails = [a.email for a in va_list]
        assert f"vikram_{run_id}@tenantcorp.com" in va_emails, "Created voice agent must be listed for tenant!"

        # 5. Create an AI Agent under this tenant
        created_ai = await create_agent(
            payload=AgentCreate(
                name=f"AI Sales Bot {run_id}",
                type="SALES",
                system_prompt="Helpful sales agent"
            ),
            user=tenant_admin,
            db=db
        )
        assert created_ai.business_id == tenant_id

        # 6. Verify AI Agent is listed for this tenant
        ai_list = await list_agents(user=tenant_admin, db=db)
        ai_names = [a.name for a in ai_list]
        assert f"AI Sales Bot {run_id}" in ai_names, "Created AI agent must be listed for tenant!"

        print(f"SUCCESS: Tenant {tenant_id} successfully created and isolated Voice Agent & AI Agent.")

        # Cleanup
        await db.delete(created_ai)
        await db.delete(created_va)
        await db.delete(tenant_admin)
        await db.delete(biz)
        await db.commit()
