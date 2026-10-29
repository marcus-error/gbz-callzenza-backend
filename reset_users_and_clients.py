import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Business, User

async def reset_users_and_clients():
    settings = get_settings()
    engine = create_async_engine(settings.database_url, echo=False)
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with SessionLocal() as db:
        # Clear tables
        await db.execute(text("DELETE FROM users;"))
        await db.execute(text("DELETE FROM businesses;"))
        await db.execute(text("DELETE FROM client_configs;"))
        await db.commit()

        # Seed default Business
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
            print(f"Created Admin User: admin@callzenza.com")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(reset_users_and_clients())
