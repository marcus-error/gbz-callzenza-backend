import asyncio
from app.db.database import AsyncSessionLocal
from app.db.models import CallParticipant
import uuid

async def test():
    async with AsyncSessionLocal() as db:
        cp = CallParticipant(
            id=str(uuid.uuid4()),
            call_id=str(uuid.uuid4()),
            participant_type="CUSTOMER",
            participant_id=str(uuid.uuid4()),
            phone_number="1234567890"
        )
        db.add(cp)
        try:
            await db.commit()
            print("SUCCESS")
        except Exception as e:
            print(f"ERROR: {e}")

asyncio.run(test())
