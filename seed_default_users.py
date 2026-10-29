import asyncio
from sqlalchemy import select
from app.db.database import AsyncSessionLocal, init_db
from app.db.models import Business, User
from app.core.security import hash_password

async def main():
    await init_db()
    async with AsyncSessionLocal() as db:
        biz = await db.scalar(select(Business))
        if not biz:
            biz = Business(
                name="Default Business",
                industry="Technology",
                owner_user_id="temp_owner",
                is_active=True
            )
            db.add(biz)
            await db.flush()

        admin = await db.scalar(select(User).where(User.email == "admin@callzenza.com"))
        if not admin:
            admin = User(
                email="admin@callzenza.com",
                hashed_password=hash_password("admin123"),
                plain_password="admin123",
                full_name="System Admin",
                role="SUPER_ADMIN",
                is_active=True,
                business_id=biz.id
            )
            db.add(admin)
            await db.flush()
            biz.owner_user_id = admin.id
            print("Seeded admin@callzenza.com (password: admin123)")
        else:
            admin.hashed_password = hash_password("admin123")
            admin.plain_password = "admin123"
            admin.is_active = True
            print("Updated admin@callzenza.com password to admin123")
        await db.commit()

if __name__ == "__main__":
    asyncio.run(main())
