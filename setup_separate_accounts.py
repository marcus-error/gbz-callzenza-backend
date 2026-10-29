import asyncio
from sqlalchemy import select, text
from app.db.database import AsyncSessionLocal
from app.db.models import Business, User
from app.core.security import hash_password

async def setup_separate_accounts():
    async with AsyncSessionLocal() as db:
        # 1. Check or retrieve primary business
        biz = await db.scalar(select(Business))
        biz_id = biz.id if biz else "c3dce06c-8330-4724-ba33-b5282b3c3596"

        # 2. Configure Regular Admin (Tenant Admin)
        # Email: admin@callzenza.com
        # Password: admin123
        # Role: ADMIN
        admin_user = await db.scalar(select(User).where(User.email == "admin@callzenza.com"))
        if admin_user:
            admin_user.role = "ADMIN"
            admin_user.hashed_password = hash_password("admin123")
            admin_user.plain_password = "admin123"
            admin_user.is_active = True
            admin_user.email_verified = True
            db.add(admin_user)
            print("[OK] Tenant Admin (admin@callzenza.com) updated with role=ADMIN, password=admin123")
        else:
            admin_user = User(
                email="admin@callzenza.com",
                hashed_password=hash_password("admin123"),
                plain_password="admin123",
                full_name="Tenant Admin",
                role="ADMIN",
                is_active=True,
                email_verified=True,
                business_id=biz_id,
            )
            db.add(admin_user)
            print("[OK] Tenant Admin (admin@callzenza.com) created with role=ADMIN, password=admin123")

        # 3. Configure Dedicated Platform Super Admin
        # Email: superadmin@callzenza.com
        # Password: superadmin123 (Different password from admin123!)
        # Role: SUPER_ADMIN
        super_user = await db.scalar(select(User).where(User.email == "superadmin@callzenza.com"))
        if super_user:
            super_user.role = "SUPER_ADMIN"
            super_user.hashed_password = hash_password("superadmin123")
            super_user.plain_password = "superadmin123"
            super_user.is_active = True
            super_user.email_verified = True
            db.add(super_user)
            print("[OK] Super Admin (superadmin@callzenza.com) updated with role=SUPER_ADMIN, password=superadmin123")
        else:
            super_user = User(
                email="superadmin@callzenza.com",
                hashed_password=hash_password("superadmin123"),
                plain_password="superadmin123",
                full_name="Platform Super Admin",
                role="SUPER_ADMIN",
                is_active=True,
                email_verified=True,
                business_id=biz_id,
            )
            db.add(super_user)
            print("[OK] Super Admin (superadmin@callzenza.com) created with role=SUPER_ADMIN, password=superadmin123")

        await db.commit()
        print("\n=== SUCCESS: Separate Accounts Configured ===")
        print("1. Regular Tenant Admin: admin@callzenza.com | Password: admin123 | Role: ADMIN")
        print("2. Platform Super Admin: superadmin@callzenza.com | Password: superadmin123 | Role: SUPER_ADMIN")

if __name__ == "__main__":
    asyncio.run(setup_separate_accounts())
