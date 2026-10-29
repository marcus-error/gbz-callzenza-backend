import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        res = await db.scalars(select(User))
        for u in res.all():
            print(f"Role: {u.role} | Email: {u.email} | ID: {u.id} | HasHashedPw: {bool(u.hashed_password)}")

if __name__ == "__main__":
    asyncio.run(main())
