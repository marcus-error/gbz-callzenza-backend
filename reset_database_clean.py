import asyncio
import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Business, User

async def clean_reset_database():
    """Wipes all users, agents, scripts, campaigns, leads, calls, and client data, resetting the system to a clean initial state."""
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    tables_to_clear = [
        "messages",
        "conversations",
        "chat_messages",
        "chat_conversations",
        "call_messages",
        "transcripts",
        "call_participants",
        "call_transfers",
        "calls",
        "parked_calls",
        "parked_conferences",
        "hopper_entries",
        "recycle_lists",
        "campaign_leads",
        "campaigns",
        "lead_assignments",
        "agent_assignment_queue",
        "leads",
        "lead_groups",
        "qualification_rules",
        "deals",
        "appointments",
        "ai_agents",
        "experts",
        "agents",
        "agent_sessions",
        "session_states",
        "dialer_runs",
        "dial_jobs",
        "mgmt_voice_agent_users",
        "mgmt_ai_agent_users",
        "mgmt_voice_agents",
        "mgmt_ai_agents",
        "mgmt_agents",
        "user_groups",
        "group_members",
        "client_configs",
        "clients",
        "users",
        "businesses",
    ]

    async with SessionLocal() as db:
        print("Clearing all data tables...")
        for table in tables_to_clear:
            try:
                await db.execute(text(f"DELETE FROM {table};"))
            except Exception as e:
                print(f"Warning clearing table '{table}': {e}")
        await db.commit()
        print("All database tables cleared successfully.")

        # Seed default Business
        biz = Business(
            name="Default Business",
            industry="Technology",
            owner_user_id="temp_owner",
            is_active=True
        )
        db.add(biz)
        await db.flush()

        admin_pw = os.getenv("INITIAL_ADMIN_PASSWORD", "superadmin123")
        admin_user = User(
            email="superadmin@callzenza.com",
            hashed_password=hash_password(admin_pw),
            plain_password=admin_pw,
            full_name="Platform Super Admin",
            role="SUPER_ADMIN",
            is_active=True,
            business_id=biz.id
        )
        db.add(admin_user)
        await db.flush()

        biz.owner_user_id = admin_user.id
        await db.commit()

        print("--------------------------------------------------")
        print("Database reset complete! Fresh initial state created.")
        print(f"Admin User: superadmin@callzenza.com")
        print(f"Password:   {admin_pw}")
        print("--------------------------------------------------")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(clean_reset_database())
