import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import User
from sqlalchemy import select
from app.core.security import verify_password

async def main():
    async with AsyncSessionLocal() as db:
        u = await db.scalar(select(User).where(User.email == 'super@gmail.com'))
        if u:
            print(f"User found: {u.email}")
            print(f"Plain password from DB: {u.plain_password}")
            print(f"Hashed password from DB: {u.hashed_password}")
            is_valid = verify_password(u.plain_password, u.hashed_password)
            print(f"Verification result: {is_valid}")
        else:
            print("User not found.")

if __name__ == "__main__":
    asyncio.run(main())
