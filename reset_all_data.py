import asyncio
import os
import shutil
from pathlib import Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Business, User

async def reset_database_and_files():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    tables_to_clear = [
        "campaign_leads",
        "hopper_entries",
        "recycle_lists",
        "dial_jobs",
        "dialer_runs",
        "campaigns",
        "qualification_rules",
        "lead_assignments",
        "agent_assignment_queue",
        "parked_calls",
        "parked_conferences",
        "call_transfers",
        "call_participants",
        "call_messages",
        "transcripts",
        "session_states",
        "calls",
        "leads",
        "lead_groups",
        "conversations",
        "messages",
        "deals",
        "appointments",
        "notifications",
        "audit_logs",
        "client_configs",
        "clients",
        "mgmt_voice_agent_users",
        "mgmt_ai_agent_users",
        "mgmt_ai_agents",
        "mgmt_voice_agents",
        "mgmt_agents",
        "ai_agents",
        "agents",
        "agent_sessions",
        "experts",
        "knowledge_metadata",
        "knowledge_sources",
        "knowledge_library",
        "db_invoices",
        "system_configs",
        "users",
        "businesses",
    ]

    async with SessionLocal() as db:
        # Disable foreign key checks for clean wiping if SQLite
        if "sqlite" in settings.database_url:
            await db.execute(text("PRAGMA foreign_keys = OFF;"))

        for tbl in tables_to_clear:
            try:
                await db.execute(text(f"DELETE FROM {tbl};"))
                print(f"Cleared table: {tbl}")
            except Exception as e:
                print(f"Skipped/Error on table {tbl}: {e}")

        await db.commit()

        # Seed clean default business
        biz = Business(
            name="Default Business",
            industry="Technology",
            owner_user_id="temp_owner",
            is_active=True
        )
        db.add(biz)
        await db.flush()

        import os
        admin_pw = os.getenv("INITIAL_ADMIN_PASSWORD")
        if admin_pw:
            admin_user = User(
                email="admin@callzenza.com",
                hashed_password=hash_password(admin_pw),
                full_name="System Admin",
                role="SUPER_ADMIN",
                is_active=True,
                business_id=biz.id
            )
            db.add(admin_user)
            await db.flush()

            biz.owner_user_id = admin_user.id
        await db.commit()

        if "sqlite" in settings.database_url:
            await db.execute(text("PRAGMA foreign_keys = ON;"))

        print("\n" + "="*50)
        print("DATABASE RESET SUCCESSFUL")
        print(f"Default Business ID: {biz.id}")
        print(f"Super Admin Email:   {admin_user.email}")
        print(f"Super Admin Password: admin123")
        print("="*50 + "\n")

    await engine.dispose()

    # Clear uploaded static files
    upload_dir = Path(__file__).resolve().parent / "app" / "static" / "uploads"
    if upload_dir.exists():
        for f in upload_dir.glob("*"):
            if f.is_file():
                try:
                    f.unlink()
                    print(f"Deleted uploaded file: {f.name}")
                except Exception as e:
                    print(f"Error deleting file {f.name}: {e}")

if __name__ == "__main__":
    asyncio.run(reset_database_and_files())
