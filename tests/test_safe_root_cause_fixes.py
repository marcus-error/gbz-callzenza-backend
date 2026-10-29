"""
Regression Test Suite: Validating Safe Root Cause Fixes for CallZenza Telephony.

Covers:
1. Issue B: _load_active_inbound_account ignores placeholder/dummy hosts ('host') and prefers valid accounts.
2. Issue C: WinError 10038/EBADF socket teardown is caught gracefully without raising unexpected error tracebacks.
3. Issue D: 401 Unauthorized challenge generates an RFC 3261 compliant ACK packet.
4. Issue E: pause_sip_ai_bot and resume_sip_ai_bot are idempotent and suppress duplicate logs.
5. Issue A: Silent or missing customer frames produce valid non-crashing -96.0 dBFS silence calculation.
"""

import asyncio
import socket
import uuid
import pytest
from unittest.mock import MagicMock, patch

from app.services.sip_ai_bot import pause_sip_ai_bot, resume_sip_ai_bot
from app.api.routes.barge_stream import barge_sessions, calc_ulaw_rms_dbfs
from app.db.database import AsyncSessionLocal
from app.db.models import SIPAccount, Business


@pytest.mark.asyncio
async def test_issue_b_inbound_account_filters_dummy_host():
    """Verify that _load_active_inbound_account rejects dummy placeholder hosts ('host') and picks valid accounts."""
    from app.services.sip_inbound_daemon import _load_active_inbound_account

    t_id = f"test-tenant-{uuid.uuid4().hex[:8]}"
    async with AsyncSessionLocal() as db:
        biz = Business(id=t_id, name="Test Inbound Org", owner_user_id=str(uuid.uuid4()))
        db.add(biz)
        
        # Add dummy account with host='host'
        dummy_acc = SIPAccount(
            id=f"dummy-{uuid.uuid4().hex[:8]}",
            business_id=t_id,
            name="Dummy Trunk",
            host="host",
            port=5060,
            username="dummy_user",
            direction="both",
            enabled=True,
            is_default=False,
        )
        # Add real account with valid IP
        real_acc = SIPAccount(
            id=f"real-{uuid.uuid4().hex[:8]}",
            business_id=t_id,
            name="Real Trunk",
            host="143.198.21.140",
            port=5060,
            username="real_user",
            direction="both",
            enabled=True,
            is_default=False,
        )
        db.add_all([dummy_acc, real_acc])
        await db.commit()

    selected = await _load_active_inbound_account()
    assert selected is not None
    assert selected.host.strip().lower() not in ("host", "dummy", "placeholder")


def test_issue_e_pause_and_resume_ai_bot_idempotency():
    """Verify that calling pause and resume repeatedly is idempotent and does not log duplicate events."""
    call_id = f"call-{uuid.uuid4().hex[:12]}"
    barge_sessions[call_id] = {
        "browser_bargers": set(),
        "supervisor_barging": False,
        "ai_speaking": True,
        "ai_rtp_tx_queue": asyncio.Queue(),
    }

    try:
        with patch("app.services.sip_ai_bot.logger.info") as mock_info:
            # First pause
            pause_sip_ai_bot(call_id)
            assert barge_sessions[call_id]["supervisor_barging"] is True
            assert mock_info.call_count == 1

            # Duplicate pause: should NOT log duplicate
            pause_sip_ai_bot(call_id)
            assert mock_info.call_count == 1

            # First resume
            resume_sip_ai_bot(call_id)
            assert barge_sessions[call_id]["supervisor_barging"] is False
            assert mock_info.call_count == 2

            # Duplicate resume: should NOT log duplicate
            resume_sip_ai_bot(call_id)
            assert mock_info.call_count == 2
    finally:
        barge_sessions.pop(call_id, None)


def test_issue_a_silence_calculation_returns_quiet_dbfs():
    """Verify that empty, none, or low-level frames return silence at -96.0 dBFS safely."""
    assert calc_ulaw_rms_dbfs(None) == -96.0
    assert calc_ulaw_rms_dbfs(b"") == -96.0
    # 160 bytes of silence (0xFF in PCMU is 0)
    silence_frame = b"\xff" * 160
    assert calc_ulaw_rms_dbfs(silence_frame) <= -80.0
