import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import User
from sqlalchemy import select

async def main():
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(User).where(User.email == 'superv@gmail.com'))
        u = res.scalar()
        if u:
            print(f"User: {u.email}")
            print(f"Plain Password: '{u.plain_password}'")
            print(f"Hashed Password: '{u.hashed_password}'")
        else:
            print("Not found")

if __name__ == "__main__":
    asyncio.run(main())
