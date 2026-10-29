import asyncio
import sys
sys.path.append('.')
from app.db.database import AsyncSessionLocal
from sqlalchemy import text

async def main():
    async with AsyncSessionLocal() as db:
        result = await db.execute(text('''
            SELECT id, client_name, call_sid, status, duration_seconds, created_at 
            FROM calls 
            WHERE call_sid LIKE 'local-test-%'
            ORDER BY created_at DESC 
            LIMIT 10
        '''))
        rows = result.fetchall()
        if rows:
            for row in rows:
                print(f"id={row[0][:8]} name={row[1]!r} sid={row[2]} status={row[3]} duration={row[4]}s created={row[5]}")
        else:
            print("No local-test calls found in DB")
        
        # Also check total count
        count = await db.execute(text('SELECT COUNT(*) FROM calls'))
        print(f"\nTotal calls in DB: {count.scalar()}")

asyncio.run(main())
