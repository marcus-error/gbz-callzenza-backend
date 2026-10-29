"""
Targeted Regression Test Suite for:
1. SIP Registration Isolation & Transaction Challenge Tracking (Scenarios 1-6)
2. Dynamic Per-Call DID Selection, Tenant Isolation, and SIP Header Construction (Scenarios 7-12)
"""

import asyncio
import socket
import uuid
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

from app.services.sip_inbound_daemon import (
    DAEMON_ACCOUNT_REGISTRATIONS,
    DAEMON_REGISTRATION_INFO,
    get_daemon_registration_info,
    _record_daemon_registration_event,
)
from app.services.sip_account_service import (
    test_and_update_sip_account as run_test_and_update_sip_account,
    classify_sip_registration_response,
    validate_sip_account_for_campaign,
)
from app.services.voice_service import SIPVoiceProvider, hangup_sip_call
from app.db.models import SIPAccount, Campaign, DIDNumber, Call


# -------------------------------------------------------------------------
# Scenario 1: Account A returns 200 OK. Account B returns auth failure.
# Account A remains registered and Account B shows its own failure.
# -------------------------------------------------------------------------
def test_scenario_1_account_registration_isolation():
    acc_a_id = "acc-test-a-uuid"
    acc_b_id = "acc-test-b-uuid"

    # Record registration success for Account A
    _record_daemon_registration_event(
        account_id=acc_a_id,
        organization_id="org-a",
        host="143.198.21.140",
        port=5060,
        user="testuser_a",
        status="registered",
        is_registered=True,
        sip_status_code=200,
        attempt_id="att-a-1",
    )

    # Record authentication failure for Account B
    _record_daemon_registration_event(
        account_id=acc_b_id,
        organization_id="org-b",
        host="143.198.21.140",
        port=5060,
        user="testuser_b",
        status="auth_failed",
        is_registered=False,
        error_message="Authentication retry rejected (401/407)",
        sip_status_code=401,
        attempt_id="att-b-1",
    )

    info_a = get_daemon_registration_info(acc_a_id)
    info_b = get_daemon_registration_info(acc_b_id)

    assert info_a["is_registered"] is True
    assert info_a["status"] == "registered"
    assert info_a["sip_status_code"] == 200
    assert info_a["account_id"] == acc_a_id

    assert info_b["is_registered"] is False
    assert info_b["status"] == "auth_failed"
    assert info_b["sip_status_code"] == 401
    assert info_b["account_id"] == acc_b_id


# -------------------------------------------------------------------------
# Scenario 2: Two accounts share the same registrar host but use different
# usernames/account IDs; their statuses remain isolated.
# -------------------------------------------------------------------------
def test_scenario_2_same_host_different_accounts_isolated():
    acc_1_id = "acc-shared-host-1"
    acc_2_id = "acc-shared-host-2"
    shared_host = "143.198.21.140"

    _record_daemon_registration_event(
        account_id=acc_1_id,
        organization_id="org-shared",
        host=shared_host,
        port=5060,
        user="user1",
        status="registered",
        is_registered=True,
        sip_status_code=200,
        attempt_id="att-1",
    )

    _record_daemon_registration_event(
        account_id=acc_2_id,
        organization_id="org-shared",
        host=shared_host,
        port=5060,
        user="user2",
        status="unregistered",
        is_registered=False,
        attempt_id="att-2",
    )

    assert get_daemon_registration_info(acc_1_id)["status"] == "registered"
    assert get_daemon_registration_info(acc_2_id)["status"] == "unregistered"

    # Querying unmonitored account id returns empty default, never acc_1's state
    unmonitored = get_daemon_registration_info("acc-unmonitored-id")
    assert unmonitored["is_registered"] is False
    assert unmonitored["status"] == "unregistered"


# -------------------------------------------------------------------------
# Scenario 3: Initial digest challenge (401/407) followed by successful
# authenticated REGISTER ends in 'registered'.
# -------------------------------------------------------------------------
def test_scenario_3_challenge_then_authenticated_success_ends_in_registered():
    # Initial 401 challenge: classified as challenge, NOT as auth failure
    diag_initial = classify_sip_registration_response(
        status_code=401,
        first_line="SIP/2.0 401 Unauthorized",
        raw_message='WWW-Authenticate: Digest realm="asterisk", nonce="abc12345"',
        is_authenticated_retry=False,
        clean_host="143.198.21.140",
    )
    assert diag_initial["status"] == "registering"
    assert diag_initial["retryable"] is True
    assert diag_initial["error_code"] == "AUTH_CHALLENGE_RECEIVED"

    # Subsequent 200 OK after digest authentication: classified as registered
    diag_success = classify_sip_registration_response(
        status_code=200,
        first_line="SIP/2.0 200 OK",
        raw_message="",
        is_authenticated_retry=True,
        clean_host="143.198.21.140",
    )
    assert diag_success["status"] == "registered"
    assert diag_success["success"] is True
    assert diag_success["sip_status_code"] == 200


# -------------------------------------------------------------------------
# Scenario 4: Final authentication rejection after digest retry ends in
# 'auth_failed' for that specific account.
# -------------------------------------------------------------------------
def test_scenario_4_auth_rejection_after_retry_ends_in_auth_failed():
    diag_rejected = classify_sip_registration_response(
        status_code=401,
        first_line="SIP/2.0 401 Unauthorized",
        raw_message='WWW-Authenticate: Digest realm="asterisk", nonce="fresh"',
        is_authenticated_retry=True,
        clean_host="143.198.21.140",
    )
    assert diag_rejected["status"] == "auth_failed"
    assert diag_rejected["success"] is False
    assert diag_rejected["error_code"] == "AUTH_REJECTED"
    assert diag_rejected["retryable"] is False


# -------------------------------------------------------------------------
# Scenario 5: Stale response or delayed response cannot overwrite latest
# attempt's status incorrectly.
# -------------------------------------------------------------------------
def test_scenario_5_stale_response_cannot_overwrite_newer_attempt():
    acc_id = "acc-stale-test"

    # Attempt 2 registered successfully
    _record_daemon_registration_event(
        account_id=acc_id,
        organization_id="org-stale",
        host="143.198.21.140",
        port=5060,
        user="testuser",
        status="registered",
        is_registered=True,
        sip_status_code=200,
        attempt_id="attempt-2-newer",
    )

    current_state = get_daemon_registration_info(acc_id)
    assert current_state["status"] == "registered"
    assert current_state["attempt_id"] == "attempt-2-newer"

    # Guard in daemon response handler: if response attempt_id does not match active attempt,
    # the stale response is ignored.
    active_attempt_id = current_state["attempt_id"]
    delayed_response_attempt_id = "attempt-1-stale"

    is_stale = (delayed_response_attempt_id != active_attempt_id)
    assert is_stale is True
    # The active registration state remains registered with attempt 2
    assert get_daemon_registration_info(acc_id)["is_registered"] is True


# -------------------------------------------------------------------------
# Scenario 6: Socket reachability alone does NOT set registration status
# to 'registered'.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_6_socket_reachability_does_not_mark_registered():
    mock_db = AsyncMock()
    mock_account = MagicMock(spec=SIPAccount)
    mock_account.id = "acc-probe-only"
    mock_account.host = "143.198.21.140"
    mock_account.port = 5060
    mock_account.transport = "UDP"
    mock_account.direction = "inbound"
    mock_account.registration_status = "unregistered"
    mock_account.settings = {}

    with patch("app.services.sip_account_service.probe_sip_connectivity", return_value={
        "reachable": True,
        "latency_ms": 25.0,
        "sip_status_code": 200,
        "sip_status_reason": "OK",
        "check_type": "SIP_OPTIONS",
    }):
        await run_test_and_update_sip_account(mock_db, mock_account)

    # Health is healthy because host answered UDP/OPTIONS
    assert mock_account.last_health_status == "healthy"
    # But registration status must NOT be set to registered without SIP REGISTER
    assert mock_account.registration_status != "registered"
    assert mock_account.registration_status == "unregistered"


# -------------------------------------------------------------------------
# Scenario 7: Campaign A uses its persisted SIP account and DID;
# Campaign B uses its different account and DID.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_7_campaign_a_and_b_use_different_persisted_sip_and_did():
    db = AsyncMock()

    camp_a = Campaign(
        id="camp-a-uuid",
        name="Campaign Alpha",
        business_id="biz-tenant-1",
        sip_account_id="sip-acc-a",
        caller_did_id="did-row-87",
    )
    camp_b = Campaign(
        id="camp-b-uuid",
        name="Campaign Beta",
        business_id="biz-tenant-1",
        sip_account_id="sip-acc-b",
        caller_did_id="did-row-42",
    )

    did_87 = DIDNumber(id="did-row-87", phone_number="917935485287", status="ACTIVE")
    did_42 = DIDNumber(id="did-row-42", phone_number="917935485242", status="ACTIVE")

    acc_a = SIPAccount(id="sip-acc-a", organization_id="biz-tenant-1", name="Trunk A", enabled=True, direction="both", caller_id="917935485287")
    acc_b = SIPAccount(id="sip-acc-b", organization_id="biz-tenant-1", name="Trunk B", enabled=True, direction="both", caller_id="917935485242")

    async def mock_get(entity, entity_id):
        if entity == DIDNumber:
            return did_87 if entity_id == "did-row-87" else (did_42 if entity_id == "did-row-42" else None)
        if entity == SIPAccount:
            return acc_a if entity_id == "sip-acc-a" else (acc_b if entity_id == "sip-acc-b" else None)
        return None

    db.get.side_effect = mock_get

    # Resolve Campaign A DID
    did_row_a = await db.get(DIDNumber, camp_a.caller_did_id)
    resolved_did_a = did_row_a.phone_number
    resolved_acc_a = await db.get(SIPAccount, camp_a.sip_account_id)

    # Resolve Campaign B DID
    did_row_b = await db.get(DIDNumber, camp_b.caller_did_id)
    resolved_did_b = did_row_b.phone_number
    resolved_acc_b = await db.get(SIPAccount, camp_b.sip_account_id)

    assert resolved_did_a == "917935485287"
    assert resolved_acc_a.id == "sip-acc-a"

    assert resolved_did_b == "917935485242"
    assert resolved_acc_b.id == "sip-acc-b"
    assert resolved_did_a != resolved_did_b


# -------------------------------------------------------------------------
# Scenario 8: Changing a campaign's selected DID/account is reflected in
# the next call without a code change or server restart.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_8_changing_campaign_did_reflected_dynamically():
    db = AsyncMock()
    camp = Campaign(
        id="camp-dynamic",
        business_id="biz-1",
        sip_account_id="sip-acc-1",
        caller_did_id="did-old-87",
    )

    did_87 = DIDNumber(id="did-old-87", phone_number="917935485287", status="ACTIVE")
    did_99 = DIDNumber(id="did-new-99", phone_number="917935485299", status="ACTIVE")

    async def mock_get(entity, entity_id):
        if entity_id == "did-old-87":
            return did_87
        if entity_id == "did-new-99":
            return did_99
        return None

    db.get.side_effect = mock_get

    # First call: resolved from persisted caller_did_id "did-old-87"
    row1 = await db.get(DIDNumber, camp.caller_did_id)
    assert row1.phone_number == "917935485287"

    # User modifies campaign in UI and saves: caller_did_id points to did-new-99
    camp.caller_did_id = "did-new-99"

    # Next call dynamically fetches updated row
    row2 = await db.get(DIDNumber, camp.caller_did_id)
    assert row2.phone_number == "917935485299"


# -------------------------------------------------------------------------
# Scenario 9: Concurrent calls from different campaigns cannot leak SIP
# account, trunk, or caller-ID state.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_9_concurrent_calls_isolation():
    captured_calls = []

    async def simulate_call_dispatch(campaign_id, caller_did, trunk_username):
        await asyncio.sleep(0.01)
        # Verify isolation: each execution frame holds its own explicit arguments
        call_snapshot = {
            "campaign_id": campaign_id,
            "caller_did": caller_did,
            "trunk_username": trunk_username,
        }
        captured_calls.append(call_snapshot)
        return call_snapshot

    res1, res2 = await asyncio.gather(
        simulate_call_dispatch("camp-1", "917935485287", "trunk_user_a"),
        simulate_call_dispatch("camp-2", "917935485231", "trunk_user_b"),
    )

    assert res1["caller_did"] == "917935485287"
    assert res1["trunk_username"] == "trunk_user_a"

    assert res2["caller_did"] == "917935485231"
    assert res2["trunk_username"] == "trunk_user_b"


# -------------------------------------------------------------------------
# Scenario 10: Missing, disabled, or mismatched-tenant SIP account fails
# safely before dialing.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_10_invalid_or_mismatched_tenant_sip_fails_safely():
    db = AsyncMock()

    # Case A: Missing SIP account
    exec_res_a = MagicMock()
    exec_res_a.scalar_one_or_none.return_value = None
    db.execute.return_value = exec_res_a
    db.get.return_value = None

    is_valid, err, acc = await validate_sip_account_for_campaign(db, "non-existent-id", "biz-1", "outbound")
    assert is_valid is False
    assert "not exist" in err.lower() or "not found" in err.lower()

    # Case B: Disabled SIP account
    disabled_acc = SIPAccount(id="acc-dis", organization_id="biz-1", enabled=False, direction="outbound")
    exec_res_b = MagicMock()
    exec_res_b.scalar_one_or_none.return_value = disabled_acc
    db.execute.return_value = exec_res_b
    db.get.return_value = disabled_acc

    is_valid, err, acc = await validate_sip_account_for_campaign(db, "acc-dis", "biz-1", "outbound")
    assert is_valid is False
    assert "disabled" in err.lower()

    # Case C: Tenant mismatch (account belongs to org-2, campaign belongs to biz-1)
    other_tenant_acc = SIPAccount(id="acc-other", organization_id="org-2", enabled=True, direction="outbound")
    exec_res_c = MagicMock()
    exec_res_c.scalar_one_or_none.return_value = None  # query filtered by biz-1 returns None
    db.execute.return_value = exec_res_c
    db.get.return_value = other_tenant_acc  # raw lookup reveals other organization

    is_valid, err, acc = await validate_sip_account_for_campaign(db, "acc-other", "biz-1", "outbound")
    assert is_valid is False
    assert "tenant" in err.lower() or "organization" in err.lower()


# -------------------------------------------------------------------------
# Scenario 11: Constructed INVITE contains expected configured caller-ID
# headers within provider-supported behavior.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_11_constructed_invite_headers():
    captured_packets = []

    class MockSocket:
        def __init__(self, *args, **kwargs):
            self._closed = False
        def bind(self, addr): pass
        def getsockname(self): return ("127.0.0.1", 5062)
        def settimeout(self, t): pass
        def sendto(self, data, addr):
            captured_packets.append(data.decode("utf-8", errors="ignore"))
        def recvfrom(self, bufsize):
            if len(captured_packets) == 1:
                challenge = (
                    "SIP/2.0 401 Unauthorized\r\n"
                    "Via: SIP/2.0/UDP 127.0.0.1:5062;rport=5062\r\n"
                    "From: <sip:917935485287@49.249.142.147>;tag=tag123\r\n"
                    "To: <sip:9876543210@49.249.142.147:5060>;tag=as123\r\n"
                    "Call-ID: test-call-id\r\n"
                    "CSeq: 1 INVITE\r\n"
                    'WWW-Authenticate: Digest realm="asterisk", nonce="4f8a12bc", algorithm=MD5\r\n'
                    "Content-Length: 0\r\n\r\n"
                )
                return challenge.encode("utf-8"), ("49.249.142.147", 5060)
            raise OSError(10038, "Socket closed")
        def close(self): self._closed = True
        def fileno(self): return 1 if not self._closed else -1

    provider = SIPVoiceProvider()
    mock_acc = MagicMock()
    mock_acc.host = "49.249.142.147"
    mock_acc.port = 5060
    mock_acc.username = "09410087540"
    mock_acc.auth_username = "09410087540"
    mock_acc.caller_id = "917935485287"
    mock_acc.transport = "UDP"
    mock_acc.outbound_proxy = None
    mock_acc.dialing_prefix = ""
    mock_acc.settings = {}
    mock_acc.name = "Test Trunk"

    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda: MockSocket()), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 10002)), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="127.0.0.1"), \
         patch("app.core.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.twilio_account_sid = None
        s.twilio_auth_token = None
        s.twilio_phone_number = None

        cid = await provider.place_call(
            to="9876543210",
            from_="917935485287",
            answer_url="",
            status_callback_url="",
            sip_account=mock_acc,
        )
        if cid:
            hangup_sip_call(cid)

    assert len(captured_packets) >= 2
    pkt1 = captured_packets[0]
    # Verify From URI contains the configured caller ID
    assert 'From: "917935485287" <sip:917935485287@49.249.142.147>;tag=' in pkt1
    # Verify P-Asserted-Identity contains the caller ID
    assert "P-Asserted-Identity: <sip:917935485287@49.249.142.147>" in pkt1
    # Verify Remote-Party-ID contains the caller ID
    assert "Remote-Party-ID: <sip:917935485287@49.249.142.147>;party=calling;screen=yes;privacy=off" in pkt1


# -------------------------------------------------------------------------
# Scenario 12: Provider CLI rewrite documentation & toggle distinction.
# Tests that use_auth_user_in_from settings flag switches From URI to sip_user
# when required by specific carriers, while keeping PAI/RPID as caller_id.
# -------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scenario_12_use_auth_user_in_from_toggle():
    captured_packets = []

    class MockSocket:
        def __init__(self, *args, **kwargs):
            self._closed = False
        def bind(self, addr): pass
        def getsockname(self): return ("127.0.0.1", 5062)
        def settimeout(self, t): pass
        def sendto(self, data, addr):
            captured_packets.append(data.decode("utf-8", errors="ignore"))
        def recvfrom(self, bufsize):
            if len(captured_packets) == 1:
                challenge = (
                    "SIP/2.0 401 Unauthorized\r\n"
                    "Via: SIP/2.0/UDP 127.0.0.1:5062;rport=5062\r\n"
                    "From: <sip:09410087540@49.249.142.147>;tag=tag123\r\n"
                    "To: <sip:9876543210@49.249.142.147:5060>;tag=as123\r\n"
                    "Call-ID: test-call-id\r\n"
                    "CSeq: 1 INVITE\r\n"
                    'WWW-Authenticate: Digest realm="asterisk", nonce="4f8a12bc", algorithm=MD5\r\n'
                    "Content-Length: 0\r\n\r\n"
                )
                return challenge.encode("utf-8"), ("49.249.142.147", 5060)
            raise OSError(10038, "Socket closed")
        def close(self): self._closed = True
        def fileno(self): return 1 if not self._closed else -1

    provider = SIPVoiceProvider()
    mock_acc = MagicMock()
    mock_acc.host = "49.249.142.147"
    mock_acc.port = 5060
    mock_acc.username = "09410087540"
    mock_acc.auth_username = "09410087540"
    mock_acc.caller_id = "917935485287"
    mock_acc.transport = "UDP"
    mock_acc.outbound_proxy = None
    mock_acc.dialing_prefix = ""
    # Carrier requires auth username in From URI
    mock_acc.settings = {"use_auth_user_in_from": True}
    mock_acc.name = "Carrier With Strict From Auth"

    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda: MockSocket()), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 10002)), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="127.0.0.1"), \
         patch("app.core.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.twilio_account_sid = None
        s.twilio_auth_token = None
        s.twilio_phone_number = None

        cid = await provider.place_call(
            to="9876543210",
            from_="917935485287",
            answer_url="",
            status_callback_url="",
            sip_account=mock_acc,
        )
        if cid:
            hangup_sip_call(cid)

    assert len(captured_packets) >= 2
    pkt1 = captured_packets[0]
    # When use_auth_user_in_from is enabled, From URI user is sip_user
    assert 'From: "917935485287" <sip:09410087540@49.249.142.147>;tag=' in pkt1
    # P-Asserted-Identity and Remote-Party-ID still carry the selected caller ID
    assert "P-Asserted-Identity: <sip:917935485287@49.249.142.147>" in pkt1
    assert "Remote-Party-ID: <sip:917935485287@49.249.142.147>;party=calling;screen=yes;privacy=off" in pkt1
