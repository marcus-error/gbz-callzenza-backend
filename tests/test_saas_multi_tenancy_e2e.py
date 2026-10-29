import asyncio
import hashlib
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(backend_dir))

from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

from app.main import app
from app.db.database import AsyncSessionLocal, init_db
from app.db.models import (
    Business,
    User,
    EmailVerificationToken,
    UserInvitation,
    PasswordResetToken,
    Expert,
)
from app.models.tenant import Tenant


async def run_e2e_tests():
    print("\n=======================================================")
    print("STARTING SAAS MULTI-TENANCY END-TO-END VERIFICATION")
    print("=======================================================\n")

    await init_db()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:

        test_slug = f"acme-enterprise-saas-test"
        test_email = "owner@acmetestsaas.com"
        test_pwd = "StrongPassword123!"

        # Clean up any leftover test data
        async with AsyncSessionLocal() as session:
            old_users = (await session.scalars(select(User).where(User.email.in_([test_email, "agent.bob@acmetestsaas.com"])))).all()
            for u in old_users:
                await session.delete(u)
            old_biz = (await session.scalars(select(Business).where(Business.slug == test_slug))).all()
            for b in old_biz:
                await session.delete(b)
            old_tenants = (await session.scalars(select(Tenant).where(Tenant.slug == test_slug))).all()
            for t in old_tenants:
                await session.delete(t)
            await session.commit()

        # Step 1: Check slug availability
        print("[TEST 1] Checking slug availability...")
        res = await client.get(f"/api/auth/check-slug?slug={test_slug}")
        assert res.status_code == 200, f"Check slug failed: {res.text}"
        slug_data = res.json()
        assert slug_data["available"] is True
        print(f"[OK] Slug check passed: {slug_data['slug']} is available.")

        # Step 2: Register Organization
        print("\n[TEST 2] Registering organization from public signup flow...")
        signup_payload = {
            "company_name": "Acme Enterprise Corp",
            "slug": test_slug,
            "first_name": "Sarah",
            "last_name": "Connor",
            "email": test_email,
            "password": test_pwd,
            "phone": "+1 555-0199",
            "country": "United States"
        }
        res = await client.post("/api/auth/register-organization", json=signup_payload)
        assert res.status_code == 201, f"Signup failed: {res.text}"
        reg_data = res.json()
        business_id = reg_data["business_id"]
        assert reg_data["slug"] == test_slug
        assert reg_data["verification_required"] is True
        print(f"✓ Organization registered successfully: Business ID = {business_id}, Slug = {reg_data['slug']}")

        # Verify DB state before verification
        async with AsyncSessionLocal() as session:
            biz = await session.get(Business, business_id)
            assert biz is not None
            assert biz.status == "pending_verification"

            owner = await session.scalar(select(User).where(User.email == test_email))
            assert owner is not None
            assert owner.role in ("ADMIN", "BUSINESS_OWNER")
            assert owner.email_verified is False
            assert owner.status == "PENDING_VERIFICATION"

            # Retrieve verification token from DB
            token_record = await session.scalar(
                select(EmailVerificationToken).where(
                    EmailVerificationToken.user_id == owner.id,
                    EmailVerificationToken.is_used == False
                )
            )
            assert token_record is not None
            print(f"✓ DB state verified: Business is 'pending_verification', Owner email_verified is False.")

        # Step 3: Verify login is blocked prior to email verification
        print("\n[TEST 3] Testing unverified login block...")
        res = await client.post("/api/auth/login", json={"email": test_email, "password": test_pwd})
        assert res.status_code == 403, f"Expected 403 for unverified user, got: {res.status_code}"
        assert "verify your email" in res.json().get("message", "").lower()
        print("✓ Login correctly rejected with 403 Forbidden because email is unverified.")

        # Step 4: Verify email address
        print("\n[TEST 4] Activating organization via /verify-email...")
        # To get the raw token, since we stored the SHA-256 hash, let's test with a fresh token or simulate verification
        # Let's create a known raw token and test the endpoint
        import secrets
        raw_verify_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_verify_token.encode()).hexdigest()

        async with AsyncSessionLocal() as session:
            token_record.token_hash = token_hash
            session.add(token_record)
            await session.commit()

        res = await client.post("/api/auth/verify-email", json={"token": raw_verify_token})
        assert res.status_code == 200, f"Verify email failed: {res.text}"
        verify_data = res.json()
        assert verify_data["success"] is True
        owner_token = verify_data["access_token"]
        print("✓ Email verified successfully! Received owner JWT token.")

        # Verify DB state after verification
        async with AsyncSessionLocal() as session:
            biz = await session.get(Business, business_id)
            assert biz.status == "active"
            owner = await session.scalar(select(User).where(User.email == test_email))
            assert owner.email_verified is True
            assert owner.status == "ACTIVE"
            print("✓ DB state verified: Business is now 'active', User email_verified is True.")

        # Step 5: Test login now succeeds
        print("\n[TEST 5] Testing login post-verification...")
        res = await client.post("/api/auth/login", json={"email": test_email, "password": test_pwd})
        assert res.status_code == 200, f"Login failed: {res.text}"
        login_data = res.json()
        assert login_data["role"] in ("ADMIN", "BUSINESS_OWNER")
        assert login_data["business_id"] == business_id
        owner_jwt = login_data["access_token"]
        print(f"✓ Login succeeded! Role = {login_data['role']}, Tenant = {login_data['business_id']}")

        # Step 6: Invite team member (no plaintext password)
        print("\n[TEST 6] Inviting team member as Voice Agent without plaintext password...")
        agent_email = "agent.bob@acmetestsaas.com"
        headers = {"Authorization": f"Bearer {owner_jwt}"}
        res = await client.post(
            "/api/tenant/users/invite",
            json={"email": agent_email, "full_name": "Bob Agent", "role": "AGENT"},
            headers=headers
        )
        assert res.status_code == 201, f"Invite failed: {res.text}"
        invite_res = res.json()
        assert "invite_link" in invite_res
        assert "temp_password" not in invite_res or invite_res.get("temp_password") is None
        print(f"✓ Team member invited securely! Invite link = {invite_res['invite_link']}")

        # Step 7: Accept team member invitation
        print("\n[TEST 7] Accepting team member invitation & setting password...")
        # Extract token from invite_link
        raw_inv_token = invite_res["invite_link"].split("token=")[-1]

        # Get invitation details
        res = await client.get(f"/api/auth/invitation-details?token={raw_inv_token}")
        assert res.status_code == 200, f"Get invitation details failed: {res.text}"
        inv_details = res.json()
        assert inv_details["email"] == agent_email
        assert inv_details["business_name"] == "Acme Enterprise Corp"
        print(f"✓ Verified invitation details for {inv_details['email']} at {inv_details['business_name']}")

        # Accept and set personal password
        agent_pwd = "AgentStrongPass99!"
        res = await client.post(
            "/api/auth/accept-invitation",
            json={"token": raw_inv_token, "password": agent_pwd, "full_name": "Bob Agent"}
        )
        assert res.status_code == 200, f"Accept invitation failed: {res.text}"
        accept_data = res.json()
        assert accept_data["success"] is True
        print(f"✓ Invitation accepted successfully! Agent account activated.")

        # Confirm agent has Expert entry for Voice Agent page
        async with AsyncSessionLocal() as session:
            agent_user = await session.scalar(select(User).where(User.email == agent_email))
            assert agent_user is not None
            assert agent_user.email_verified is True
            assert agent_user.status == "ACTIVE"

            expert = await session.scalar(select(Expert).where(Expert.user_id == agent_user.id))
            assert expert is not None
            assert expert.business_id == business_id
            print(f"✓ Voice Agent verified: Expert profile created automatically for voice agent page!")

        # Step 8: Test agent login
        print("\n[TEST 8] Testing Agent login with newly created password...")
        res = await client.post("/api/auth/login", json={"email": agent_email, "password": agent_pwd})
        assert res.status_code == 200, f"Agent login failed: {res.text}"
        agent_login = res.json()
        assert agent_login["role"] == "VOICE_AGENT"
        assert agent_login["business_id"] == business_id
        print(f"✓ Agent login succeeded! Authenticated with isolated business_id = {agent_login['business_id']}")

        # Step 9: Test password reset flow
        print("\n[TEST 9] Testing self-serve password reset...")
        res = await client.post("/api/auth/forgot-password", json={"email": test_email})
        assert res.status_code == 200
        print("✓ Forgot password requested successfully.")

        # Retrieve the reset token
        async with AsyncSessionLocal() as session:
            owner = await session.scalar(select(User).where(User.email == test_email))
            reset_record = await session.scalar(
                select(PasswordResetToken).where(
                    PasswordResetToken.user_id == owner.id,
                    PasswordResetToken.is_used == False
                ).order_by(PasswordResetToken.created_at.desc())
            )
            assert reset_record is not None

        new_raw_reset = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(new_raw_reset.encode()).hexdigest()
        async with AsyncSessionLocal() as session:
            reset_record.token_hash = new_hash
            session.add(reset_record)
            await session.commit()

        new_password = "BrandNewSuperPass123!"
        res = await client.post("/api/auth/reset-password", json={"token": new_raw_reset, "new_password": new_password})
        assert res.status_code == 200, f"Reset password failed: {res.text}"
        print("✓ Password successfully reset!")

        # Log in with new password
        res = await client.post("/api/auth/login", json={"email": test_email, "password": new_password})
        assert res.status_code == 200, f"Login with new password failed: {res.text}"
        print("✓ Successfully logged in with new password!")

        print("\n=======================================================")
        print("ALL SAAS MULTI-TENANCY E2E TESTS PASSED PERFECTLY!")
        print("=======================================================\n")


if __name__ == "__main__":
    asyncio.run(run_e2e_tests())
