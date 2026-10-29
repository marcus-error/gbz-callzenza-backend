import asyncio
import os
import glob
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Business, User, Expert

async def clear_all_test_data():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with SessionLocal() as db:
        print("Clearing all dummy clients, campaigns, leads, calls, transfers, deals, and test users...")
        tables = [
            "campaign_leads", "campaigns", "lead_assignments", "agent_assignment_queue",
            "call_transfers", "parked_calls", "call_messages", "transcripts", "calls",
            "leads", "lead_groups", "clients", "deals", "appointments", "notifications", "audit_logs"
        ]
        for table in tables:
            try:
                await db.execute(text(f"DELETE FROM {table};"))
            except Exception as e:
                print(f"Skipped {table}: {e}")

        # Delete any test users created during pytest or manual runs (keep admin@callzenza.com and jon@gmail.com)
        try:
            await db.execute(text("DELETE FROM users WHERE email NOT IN ('admin@callzenza.com', 'jon@gmail.com');"))
            await db.execute(text("DELETE FROM experts WHERE email NOT IN ('jon@gmail.com');"))
        except Exception as e:
            print(f"Error cleaning users/experts: {e}")

        await db.commit()

        # Check or recreate default Business
        biz = (await db.execute(text("SELECT id FROM businesses LIMIT 1;"))).scalars().first()
        if not biz:
            biz_obj = Business(
                name="Default Business",
                industry="Technology",
                owner_user_id="temp_owner",
                is_active=True
            )
            db.add(biz_obj)
            await db.flush()
            biz_id = biz_obj.id
        else:
            biz_id = biz

        admin_pw = os.getenv("INITIAL_ADMIN_PASSWORD")
        if admin_pw:
            await db.execute(text("UPDATE users SET hashed_password=:h WHERE email='admin@callzenza.com';"), {"h": hash_password(admin_pw)})
            admin = (await db.execute(text("SELECT id FROM users WHERE email='admin@callzenza.com';"))).scalars().first()
            if not admin:
                admin_user = User(
                    email="admin@callzenza.com",
                    hashed_password=hash_password(admin_pw),
                    full_name="System Admin",
                    role="SUPER_ADMIN",
                    is_active=True,
                    business_id=biz_id
                )
                db.add(admin_user)
                await db.flush()

        agent_pw = os.getenv("VOICE_AGENT_PASSWORD")
        if agent_pw:
            await db.execute(text("UPDATE users SET hashed_password=:h WHERE email='jon@gmail.com';"), {"h": hash_password(agent_pw)})
            jon = (await db.execute(text("SELECT id FROM users WHERE email='jon@gmail.com';"))).scalars().first()
            if not jon:
                jon_user = User(
                    email="jon@gmail.com",
                    hashed_password=hash_password(agent_pw),
                    full_name="ageon targareyoun",
                    role="VOICE_AGENT",
                    is_active=True,
                    business_id=biz_id
                )
                db.add(jon_user)
                await db.flush()

        # Reset Expert status for Ageon Targareyoun
        await db.execute(text("UPDATE experts SET status='AVAILABLE', active_lead_count=0;"))
        await db.commit()

        # Remove uploaded static files
        upload_dir = os.path.join("app", "static", "uploads")
        if os.path.exists(upload_dir):
            for f in os.listdir(upload_dir):
                if f.endswith(".xlsx") or f.endswith(".csv"):
                    try:
                        os.remove(os.path.join(upload_dir, f))
                    except Exception:
                        pass

        print("SUCCESSFULLY CLEARED ALL TEST DATA AND SEEDED CLEAN VOICE AGENT ACCOUNT!")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(clear_all_test_data())
