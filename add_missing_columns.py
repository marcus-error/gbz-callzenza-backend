import asyncio
from sqlalchemy import text
from app.db.database import engine

async def main():
    async with engine.begin() as conn:
        try:
            await conn.execute(text("ALTER TABLE call_transfers ADD COLUMN duration_seconds INTEGER DEFAULT 0;"))
            print("Added duration_seconds to call_transfers")
        except Exception as e:
            print(f"duration_seconds error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
