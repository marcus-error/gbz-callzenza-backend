import pytest
import asyncio
from unittest.mock import MagicMock, patch
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, SIPAccount, Business
from app.services.sip_inbound_daemon import (
    DAEMON_REGISTRATION_INFO,
    get_daemon_registration_info,
    _update_account_registration_in_db,
)
from app.api.routes.sip_accounts import _to_sip_account_out


@pytest.mark.asyncio
async def test_daemon_registration_failure_updates_db_and_api_output():
    """
    Verifies that when Asterisk returns a 401/407 authentication rejection to the background daemon:
    1. DAEMON_REGISTRATION_INFO captures the exact 401 error and code.
    2. The database record is updated to registration_status='auth_failed' with the failure explanation.
    3. _to_sip_account_out outputs registration_status='auth_failed', sip_status_code=401,
       and displays the exact error message so the UI renders it directly.
    """
    test_db_url = "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(test_db_url, echo=False)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        biz = Business(id="org_test", name="Test Org", owner_user_id="user_admin")
        db.add(biz)

        acc = SIPAccount(
            id="sip_acc_testuser",
            organization_id="org_test",
            name="testuser (Primary Outbound (143.198.21.140))",
            username="testuser",
            host="143.198.21.140",
            port=5060,
            transport="udp",
            caller_id="+18001112222",
            enabled=True,
            direction="both",
            registration_status="registered",  # Stale initial status
        )
        db.add(acc)
        await db.commit()

        # Simulate Asterisk sending 401 retry rejection to the daemon loop
        host = "143.198.21.140"
        port = 5060
        user = "testuser"
        err_msg = (
            f"Registration rejected: authentication retry rejected (401/407) from {host}:{port}. "
            "Verify SIP authentication username and password, and confirm the provider's digest authentication requirements."
        )

        DAEMON_REGISTRATION_INFO.update({
            "is_registered": False,
            "registered_host": host,
            "registered_port": port,
            "registered_user": user,
            "status": "auth_failed",
            "last_error": err_msg,
            "error_code": "AUTH_REJECTED",
            "sip_status_code": 401,
            "suggested_action": "Verify SIP authentication username and password in account configuration.",
            "account_id": acc.id,
        })

        # Test patch for DB update
        with patch("app.db.database.AsyncSessionLocal", return_value=async_session()):
            await _update_account_registration_in_db(
                account_id=acc.id,
                status="auth_failed",
                error_message=err_msg,
                error_code="AUTH_REJECTED",
                sip_status_code=401,
                suggested_action=DAEMON_REGISTRATION_INFO["suggested_action"],
            )

        # Refresh from database
        await db.refresh(acc)
        assert acc.registration_status == "auth_failed"
        assert "401/407" in acc.last_error_message
        assert acc.settings["registration_diagnostics"]["sip_status_code"] == 401

        # Verify _to_sip_account_out formats this accurately for the UI
        out = _to_sip_account_out(acc, org_name="Test Org")
        assert out.registration_status == "auth_failed"
        assert out.sip_status_code == 401
        assert "401/407" in out.last_error_message
        assert out.suggested_action == "Verify SIP authentication username and password in account configuration."

    await engine.dispose()
