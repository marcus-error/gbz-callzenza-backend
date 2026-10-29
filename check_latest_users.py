import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        res = await db.scalars(select(User).order_by(User.created_at.desc()).limit(5))
        for u in res.all():
            print(f"[{u.created_at}] Role: {u.role} | Email: {u.email} | Plain: '{u.plain_password}' | Hashed: {bool(u.hashed_password)}")

if __name__ == "__main__":
    asyncio.run(main())
