import asyncio
import sys
sys.path.append('.')

from app.db.database import AsyncSessionLocal
from app.db.models import User, Expert
from sqlalchemy import select, delete

async def main():
    async with AsyncSessionLocal() as db:
        users = (await db.scalars(select(User))).all()
        for u in users:
            email_lower = u.email.lower().strip()
            if email_lower in ["jon@gmail.com", "sinclair@gmail.com"] or "agent" in email_lower:
                u.role = "VOICE_AGENT"
            elif email_lower in ["admin@callmira.com", "admin@callzenza.com"]:
                u.role = "SUPER_ADMIN"
            elif email_lower == "mugilanviswanathan1711@gmail.com":
                u.role = "ADMIN"
            elif email_lower == "user@gmail.com":
                u.role = "USER"
            print(f"User {u.email} -> Role: {u.role}")

        await db.commit()

        # Remove Expert records linked to non-VOICE_AGENT users (supervisors, admins, system users)
        experts = (await db.scalars(select(Expert))).all()
        for exp in experts:
            u = None
            if exp.user_id:
                u = await db.get(User, exp.user_id)
            if not u and exp.email:
                u = (await db.scalars(select(User).where(User.email == exp.email))).first()

            if u and u.role and u.role.upper() not in ["VOICE_AGENT", "AGENT", "INBOUND_AGENT", "OUTBOUND_AGENT"]:
                print(f"Removing non-voice-agent Expert entry for: {exp.name} ({exp.email}) [Role: {u.role}]")
                await db.delete(exp)
            elif not u and any(k in (exp.email or "").lower() for k in ["admin@", "superv@"]):
                print(f"Removing orphan admin/supervisor Expert entry for: {exp.name} ({exp.email})")
                await db.delete(exp)

        await db.commit()
        print("Role separation and Expert cleanup complete.")

if __name__ == "__main__":
    asyncio.run(main())
