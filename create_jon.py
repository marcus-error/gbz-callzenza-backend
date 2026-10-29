import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import User, Business
from app.core.security import hash_password
from sqlalchemy.future import select

async def create_jon():
    async with AsyncSessionLocal() as session:
        # Get first business
        biz = (await session.execute(select(Business))).scalars().first()
        biz_id = biz.id if biz else 1
        
        # Check if Jon exists
        jon = (await session.execute(select(User).filter(User.email == 'jon@gmail.com'))).scalars().first()
        import os
        agent_pw = os.getenv("VOICE_AGENT_PASSWORD")
        if agent_pw and not jon:
            jon_user = User(
                email="jon@gmail.com",
                hashed_password=hash_password(agent_pw.strip()),
                full_name="Ageon Targareyoun",
                role="VOICE_AGENT",
                is_active=True,
                business_id=biz_id
            )
            session.add(jon_user)
            await session.commit()
            print("Successfully created jon@gmail.com with password '87654321'")
        else:
            print("User jon@gmail.com already exists!")

if __name__ == "__main__":
    asyncio.run(create_jon())
