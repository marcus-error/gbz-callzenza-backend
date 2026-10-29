"""
Comprehensive Regression & Unit Test Suite for:
1. Dynamic SIP Account Configuration & Switching (Account A vs Account B)
2. RFC 3261 Digest Authentication, 401 Challenge vs Terminal Rejection, Via Branch & Compact Headers
3. SIP Registration Diagnostics (Registered, Auth Failed, DNS Error, Network Error, Outbound Ready)
4. Edge TTS ON/OFF Operational Toggle & Persistence
5. Voice Preview Provider Isolation & Granular Error Surfacing
"""
import asyncio
import hashlib
import re
import socket
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.db.models import SIPAccount
from app.schemas.sip_account import SIPAccountCreate, SIPAccountUpdate
from app.services.sip_account_service import (
    _perform_sip_register_probe,
    classify_sip_registration_response,
    make_sip_diagnostic_error,
    decrypt_sip_secret,
    encrypt_sip_secret,
)
from app.api.routes.sip_accounts import _to_sip_account_out
from app.services.sip_inbound_daemon import (
    _extract_header_val,
    build_register_packet,
    compute_digest_auth,
)
from app.services.tts_service import (
    is_edge_tts_enabled,
    set_edge_tts_enabled,
    synthesize_edge_speech,
    synthesize_from_voice_config,
)


# ============================================================================
# 1. SIP Account Validation & Placeholder Rejection
# ============================================================================

def test_placeholder_host_rejection():
    """Verify that placeholder hostnames (host, test, dummy, placeholder, example) are strictly rejected."""
    for placeholder in ["host", "test", "dummy", "placeholder", "example", "HOST", "Test", "dummy.local"]:
        with pytest.raises(ValidationError):
            SIPAccountCreate(
                name="Test Account",
                username="1001",
                host=placeholder,
                port=5060,
                direction="outbound",
            )

    # Valid real IP and hostnames must be accepted
    acc = SIPAccountCreate(
        name="Valid Carrier Trunk",
        username="09410697548",
        host="49.249.142.147",
        port=5060,
        direction="both",
    )
    assert acc.host == "49.249.142.147"

    acc_domain = SIPAccountCreate(
        name="Valid Domain Trunk",
        username="sipuser",
        host="sip.telecom-carrier.com",
        port=5060,
        direction="outbound",
    )
    assert acc_domain.host == "sip.telecom-carrier.com"


# ============================================================================
# 2. Via Branch Extraction & Compact SIP Headers (RFC 3261)
# ============================================================================

def test_compact_sip_headers_and_case_insensitive_via_branch():
    """Verify parser supports standard and compact RFC 3261 headers and case-insensitive branch."""
    # Standard headers with uppercase Branch
    sip_standard = (
        "SIP/2.0 401 Unauthorized\r\n"
        "Via: SIP/2.0/UDP 192.168.1.100:5060;Branch=z9hG4bK-abc12345;rport=5060\r\n"
        "From: <sip:user@sip.example.com>;tag=12345\r\n"
        "To: <sip:user@sip.example.com>\r\n"
        "Call-ID: call-98765@callzenza\r\n"
        "CSeq: 1 REGISTER\r\n"
        "WWW-Authenticate: Digest realm=\"asterisk\", nonce=\"d41d8cd98f00b204e9800998ecf8427e\", algorithm=MD5\r\n"
        "Content-Length: 0\r\n\r\n"
    )

    via_val = _extract_header_val(sip_standard, "Via")
    assert "192.168.1.100:5060" in via_val
    branch_m = re.search(r"branch=([^;\s]+)", via_val, re.IGNORECASE)
    assert branch_m is not None
    assert branch_m.group(1) == "z9hG4bK-abc12345"

    cid_val = _extract_header_val(sip_standard, "Call-ID")
    assert cid_val == "call-98765@callzenza"

    # Compact headers (RFC 3261 Section 7.3.3: v, f, t, i, m, l, c)
    sip_compact = (
        "SIP/2.0 200 OK\r\n"
        "v: SIP/2.0/UDP 10.0.0.5:5060;branch=z9hG4bK-compactbranch99;rport\r\n"
        "f: <sip:caller@carrier.com>;tag=fromtag\r\n"
        "t: <sip:callee@carrier.com>;tag=totag\r\n"
        "i: compact-cid-112233@callzenza\r\n"
        "CSeq: 2 REGISTER\r\n"
        "l: 0\r\n\r\n"
    )

    compact_via = _extract_header_val(sip_compact, "Via")
    assert "10.0.0.5:5060" in compact_via
    compact_branch_m = re.search(r"branch=([^;\s]+)", compact_via, re.IGNORECASE)
    assert compact_branch_m is not None
    assert compact_branch_m.group(1) == "z9hG4bK-compactbranch99"

    compact_cid = _extract_header_val(sip_compact, "Call-ID")
    assert compact_cid == "compact-cid-112233@callzenza"


# ============================================================================
# 3. Digest Authentication Computation & Auth Username
# ============================================================================

def test_compute_digest_auth_with_separate_auth_username():
    """Verify compute_digest_auth calculates correct MD5 response using auth_user if distinct."""
    # When username is phone number but auth_username is extension/login
    user = "09410697548"
    auth_user = "agent_login_01"
    pwd = "secretpassword"
    realm = "asterisk"
    nonce = "66f4e1a0"
    uri = "sip:49.249.142.147:5060"

    auth_header = compute_digest_auth(
        user=user,
        pwd=pwd,
        realm=realm,
        nonce=nonce,
        uri=uri,
        auth_user=auth_user,
    )

    assert f'username="{auth_user}"' in auth_header
    assert f'realm="{realm}"' in auth_header
    assert f'nonce="{nonce}"' in auth_header
    assert 'algorithm=MD5' in auth_header

    # Verify digest MD5 calculation matches RFC 2617
    ha1 = hashlib.md5(f"{auth_user}:{realm}:{pwd}".encode()).hexdigest()
    ha2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
    expected_resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    assert f'response="{expected_resp}"' in auth_header


# ============================================================================
# 4. SIP Registration Probe Lifecycle & Terminal Failure Handling
# ============================================================================

def test_register_probe_placeholder_rejection():
    """Verify _perform_sip_register_probe rejects placeholder hostnames safely."""
    res = _perform_sip_register_probe(
        host="test",
        port=5060,
        username="user1",
        password="pass",
    )
    assert res["success"] is False
    assert res["status"] == "not_configured"


def test_register_probe_dns_error():
    """Verify _perform_sip_register_probe surfaces DNS resolution error cleanly without crash."""
    res = _perform_sip_register_probe(
        host="nonexistent-sip-carrier-domain-invalid-999.xyz",
        port=5060,
        username="user1",
        password="pass",
        timeout=1.0,
    )
    assert res["success"] is False
    assert res["status"] == "dns_error"
    assert "DNS" in res["error"]


def test_register_probe_challenge_and_terminal_auth_failure():
    """Mock socket interaction: initial 401 challenge, authenticated retry, followed by terminal 401."""
    # First response: 401 with WWW-Authenticate challenge
    challenge_packet = (
        b"SIP/2.0 401 Unauthorized\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-abc\r\n"
        b"From: <sip:user@test.sip>;tag=123\r\n"
        b"To: <sip:user@test.sip>\r\n"
        b"Call-ID: reg-1\r\n"
        b"CSeq: 1 REGISTER\r\n"
        b'WWW-Authenticate: Digest realm="asterisk", nonce="nonce12345", algorithm=MD5\r\n'
        b"Content-Length: 0\r\n\r\n"
    )

    # Second response: terminal 401 Unauthorized (wrong password)
    terminal_packet = (
        b"SIP/2.0 401 Unauthorized\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-def\r\n"
        b"From: <sip:user@test.sip>;tag=123\r\n"
        b"To: <sip:user@test.sip>\r\n"
        b"Call-ID: reg-1\r\n"
        b"CSeq: 2 REGISTER\r\n"
        b"Content-Length: 0\r\n\r\n"
    )

    mock_sock = MagicMock()
    # recvfrom returns challenge first, then terminal rejection
    mock_sock.recvfrom.side_effect = [
        (challenge_packet, ("127.0.0.1", 5060)),
        (terminal_packet, ("127.0.0.1", 5060)),
    ]
    mock_sock.getsockname.return_value = ("0.0.0.0", 54321)

    with patch("socket.socket", return_value=mock_sock), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 5060))]):
        res = _perform_sip_register_probe(
            host="sip.example-telecom.com",
            port=5060,
            username="testuser",
            password="badpassword",
        )

    assert res["success"] is False
    assert res["status"] == "auth_failed"
    assert "Invalid username or password" in res["error"]
    # Verify exactly two sends occurred (cseq 1 initial, cseq 2 authenticated retry)
    assert mock_sock.sendto.call_count == 2


def test_register_probe_success_200_ok():
    """Mock socket interaction: initial 401 challenge, followed by 200 OK registration success."""
    challenge_packet = (
        b"SIP/2.0 401 Unauthorized\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-abc\r\n"
        b"From: <sip:user@test.sip>;tag=123\r\n"
        b"To: <sip:user@test.sip>\r\n"
        b"Call-ID: reg-1\r\n"
        b"CSeq: 1 REGISTER\r\n"
        b'WWW-Authenticate: Digest realm="asterisk", nonce="nonce12345", algorithm=MD5\r\n'
        b"Content-Length: 0\r\n\r\n"
    )

    ok_packet = (
        b"SIP/2.0 200 OK\r\n"
        b"Via: SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bK-def\r\n"
        b"From: <sip:user@test.sip>;tag=123\r\n"
        b"To: <sip:user@test.sip>;tag=totag\r\n"
        b"Call-ID: reg-1\r\n"
        b"CSeq: 2 REGISTER\r\n"
        b"Contact: <sip:user@127.0.0.1:54321>;expires=300\r\n"
        b"Content-Length: 0\r\n\r\n"
    )

    mock_sock = MagicMock()
    mock_sock.recvfrom.side_effect = [
        (challenge_packet, ("127.0.0.1", 5060)),
        (ok_packet, ("127.0.0.1", 5060)),
    ]
    mock_sock.getsockname.return_value = ("0.0.0.0", 54321)

    with patch("socket.socket", return_value=mock_sock), \
         patch("socket.getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 5060))]):
        res = _perform_sip_register_probe(
            host="sip.example-telecom.com",
            port=5060,
            username="testuser",
            password="correctpassword",
        )

    assert res["success"] is True
    assert res["status"] == "registered"
    assert res["error"] is None
    assert "successful" in res["message"].lower()


# ============================================================================
# 5. Dynamic Switching: Account A vs Account B Outbound INVITE
# ============================================================================

@pytest.mark.asyncio
async def test_dynamic_switching_account_a_vs_account_b():
    """Verify that switching from Account A to Account B makes outbound calls use Account B's host, port, credentials, and CLI."""
    from app.services.voice_service import SIPVoiceProvider

    provider = SIPVoiceProvider()

    account_a = MagicMock()
    account_a.id = "account-a-id"
    account_a.name = "Account A (Primary)"
    account_a.host = "10.10.10.1"
    account_a.port = 5060
    account_a.username = "user_a"
    account_a.auth_username = "auth_user_a"
    account_a.password_hash = encrypt_sip_secret("password_a")
    account_a.caller_id = "+919876543210"
    account_a.transport = "UDP"
    account_a.outbound_proxy = None
    account_a.direction = "both"
    account_a.enabled = True

    account_b = MagicMock()
    account_b.id = "account-b-id"
    account_b.name = "Account B (Secondary)"
    account_b.host = "10.20.20.2"
    account_b.port = 5080
    account_b.username = "user_b"
    account_b.auth_username = None
    account_b.password_hash = encrypt_sip_secret("password_b")
    account_b.caller_id = "+919123456789"
    account_b.transport = "UDP"
    account_b.outbound_proxy = "10.20.20.254:5080"
    account_b.direction = "outbound"
    account_b.enabled = True

    captured_packets = []

    class MockTestSocket:
        def __init__(self, *args, **kwargs):
            self.timeout = None
            self.calls = 0
        def settimeout(self, t):
            self.timeout = t
        def bind(self, addr):
            pass
        def getsockname(self):
            return ("0.0.0.0", 40000)
        def sendto(self, data, dest):
            captured_packets.append((data.decode("utf-8", errors="ignore"), dest))
        def recvfrom(self, bufsize):
            return (b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n", ("10.10.10.1", 5060))
        def close(self):
            pass

    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda *a, **k: MockTestSocket()), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="49.205.80.130"), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 12000)):

        # Call 1: Using Account A
        await provider.place_call(
            to="9876543210",
            from_=account_a.caller_id,
            answer_url="https://api.callzenza.com/voice/answer?call_id=call-111",
            status_callback_url="https://api.callzenza.com/voice/status?call_id=call-111",
            sip_account=account_a,
        )

        assert len(captured_packets) >= 1
        req_a, dest_a = captured_packets[0]
        assert dest_a == ("10.10.10.1", 5060)
        assert "user_a@10.10.10.1" in req_a
        assert "+919876543210" in req_a

        # Clear captured packets
        captured_packets.clear()

        # Call 2: Switching to Account B
        await provider.place_call(
            to="9876543210",
            from_=account_b.caller_id,
            answer_url="https://api.callzenza.com/voice/answer?call_id=call-222",
            status_callback_url="https://api.callzenza.com/voice/status?call_id=call-222",
            sip_account=account_b,
        )

        assert len(captured_packets) >= 1
        req_b, dest_b = captured_packets[0]
        # Verify Account B destination used outbound proxy
        assert dest_b == ("10.20.20.254", 5080)
        assert "user_b@10.20.20.2" in req_b
        assert "+919123456789" in req_b
        assert "user_a" not in req_b


# ============================================================================
# 6. Edge TTS ON/OFF Operational Toggle & Fallback Prevention
# ============================================================================

@pytest.mark.asyncio
async def test_edge_tts_toggle_and_runtime_enforcement():
    """Verify Edge TTS can be enabled and disabled, and when disabled, prevents new synthesis."""
    mock_db = AsyncMock()

    # Enable Edge TTS
    await set_edge_tts_enabled(True, db=mock_db)
    assert await is_edge_tts_enabled(db=mock_db) is True

    # Disable Edge TTS
    await set_edge_tts_enabled(False, db=mock_db)
    assert await is_edge_tts_enabled(db=mock_db) is False

    # When disabled, synthesize_edge_speech must return empty bytes immediately
    audio_disabled = await synthesize_edge_speech("Test message while Edge TTS is disabled")
    assert audio_disabled == b""

    # Re-enable Edge TTS
    await set_edge_tts_enabled(True, db=mock_db)
    assert await is_edge_tts_enabled(db=mock_db) is True


@pytest.mark.asyncio
async def test_preview_mode_does_not_fallback_to_edge_tts():
    """Verify preview mode (for_preview=True) raises provider error directly rather than falling back to Edge TTS."""
    mock_db = AsyncMock()

    # Voice config requesting ElevenLabs with failing mock
    failing_config = {
        "provider": "elevenlabs",
        "voice_id": "test_voice_id",
        "model_id": "eleven_turbo_v2_5",
        "api_key": "invalid_test_key",
    }

    with patch("app.services.tts_service.synthesize_speech", side_effect=RuntimeError("ElevenLabs error: Invalid API key")):
        # for_preview=True must propagate provider error
        with pytest.raises(Exception) as excinfo:
            await synthesize_from_voice_config(
                text="Preview test",
                voice_config=failing_config,
                db=mock_db,
                for_preview=True,
            )
        assert "ElevenLabs" in str(excinfo.value)
        assert "Invalid API key" in str(excinfo.value)


@pytest.mark.asyncio
async def test_edge_tts_disabled_is_excluded_from_runtime_fallback():
    """Verify that when Edge TTS is disabled, runtime fallback does NOT call synthesize_edge_speech."""
    mock_db = AsyncMock()
    await set_edge_tts_enabled(False, db=mock_db)

    failing_config = {
        "provider": "murf",
        "voice_id": "Pooja",
    }

    with patch("app.services.tts_service.synthesize_murf_speech", return_value=b""), \
         patch("app.services.tts_service.synthesize_edge_speech") as mock_edge:
        # Runtime call (for_preview=False)
        audio = await synthesize_from_voice_config(
            text="Call text",
            voice_config=failing_config,
            db=mock_db,
            for_preview=False,
        )
        assert audio == b""
        # mock_edge must NOT have been called because Edge TTS is disabled
        mock_edge.assert_not_called()

    # Cleanup: restore Edge TTS enabled
    await set_edge_tts_enabled(True, db=mock_db)


# ============================================================================
# 7. SIP Registration Error Classification & Diagnostics
# ============================================================================

def test_initial_401_challenge_classification():
    """Verify initial 401 is classified as an authentication challenge, not an incorrect password."""
    challenge_hdr = 'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="sip.carrier.com", nonce="abcd1234nonce", algorithm=MD5\r\n\r\n'
    diag = classify_sip_registration_response(
        401,
        "SIP/2.0 401 Unauthorized",
        challenge_hdr,
        is_authenticated_retry=False,
    )
    assert diag["success"] is False
    assert diag["status"] == "registering"
    assert diag["error_code"] == "AUTH_CHALLENGE_RECEIVED"
    assert diag["sip_status_code"] == 401
    assert diag["retryable"] is True
    assert "challenge" in diag["message"].lower()
    # Must NOT claim password or credentials are wrong
    assert "wrong password" not in diag["message"].lower()
    assert "incorrect password" not in diag["message"].lower()


def test_repeated_401_auth_rejected_cautious_wording():
    """Verify repeated 401 after authenticated retry is reported as authentication rejection with cautious wording."""
    reject_hdr = 'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="sip.carrier.com", nonce="newnonce5678", algorithm=MD5\r\n\r\n'
    diag = classify_sip_registration_response(
        401,
        "SIP/2.0 401 Unauthorized",
        reject_hdr,
        is_authenticated_retry=True,
    )
    assert diag["success"] is False
    assert diag["status"] == "auth_failed"
    assert diag["error_code"] == "AUTH_REJECTED"
    assert diag["sip_status_code"] == 401
    assert diag["retryable"] is False
    # Verify cautious wording: does not definitively blame only the password
    assert "rejected the authenticated REGISTER request (401 Unauthorized)" in diag["message"]
    assert "The exact rejection cause has not been confirmed." in diag["message"]
    assert "wrong password" not in diag["message"].lower()


def test_distinct_diagnostics_for_all_failure_types():
    """Verify distinct error_code, status, and suggested_action for every failure condition."""
    # 1. 403 Forbidden
    diag_403 = classify_sip_registration_response(403, "SIP/2.0 403 Forbidden", "SIP/2.0 403 Forbidden\r\n\r\n", is_authenticated_retry=True)
    assert diag_403["error_code"] == "FORBIDDEN"
    assert diag_403["sip_status_code"] == 403
    assert diag_403["status"] == "auth_failed"
    assert diag_403["retryable"] is False
    assert "denied registration" in diag_403["message"]

    # 2. 404 Not Found
    diag_404 = classify_sip_registration_response(404, "SIP/2.0 404 Not Found", "SIP/2.0 404 Not Found\r\n\r\n", is_authenticated_retry=True)
    assert diag_404["error_code"] == "NOT_FOUND"
    assert diag_404["sip_status_code"] == 404
    assert diag_404["status"] == "error"
    assert diag_404["retryable"] is False

    # 3. 408 Request Timeout
    diag_408 = classify_sip_registration_response(408, "SIP/2.0 408 Request Timeout", "SIP/2.0 408 Request Timeout\r\n\r\n", is_authenticated_retry=True)
    assert diag_408["error_code"] == "REQUEST_TIMEOUT"
    assert diag_408["sip_status_code"] == 408
    assert diag_408["status"] == "network_error"
    assert diag_408["retryable"] is True

    # 4. Unsupported Digest Algorithm (e.g. SHA-256)
    unsupported_hdr = 'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="sip.carrier.com", nonce="xyz", algorithm=SHA-256\r\n\r\n'
    diag_unsupported = classify_sip_registration_response(401, "SIP/2.0 401 Unauthorized", unsupported_hdr, is_authenticated_retry=False)
    assert diag_unsupported["error_code"] == "UNSUPPORTED_DIGEST_ALGORITHM"
    assert diag_unsupported["status"] == "auth_failed"
    assert diag_unsupported["retryable"] is False
    assert "SHA-256" in diag_unsupported["message"]

    # 5. Stale Nonce
    stale_hdr = 'SIP/2.0 401 Unauthorized\r\nWWW-Authenticate: Digest realm="sip.carrier.com", nonce="oldnonce", stale=true, algorithm=MD5\r\n\r\n'
    diag_stale = classify_sip_registration_response(401, "SIP/2.0 401 Unauthorized", stale_hdr, is_authenticated_retry=True)
    assert diag_stale["error_code"] == "STALE_NONCE"
    assert diag_stale["status"] == "auth_failed"
    assert diag_stale["retryable"] is True

    # 6. Unknown SIP Error (e.g. 503 Service Unavailable)
    diag_503 = classify_sip_registration_response(503, "SIP/2.0 503 Service Unavailable", "SIP/2.0 503 Service Unavailable\r\n\r\n", is_authenticated_retry=True)
    assert diag_503["error_code"] == "UNKNOWN_SIP_ERROR"
    assert diag_503["sip_status_code"] == 503
    assert diag_503["status"] == "error"
    assert diag_503["retryable"] is True

    # 7. DNS Resolution Failure
    diag_dns = make_sip_diagnostic_error(
        status="dns_error",
        error_code="DNS_RESOLUTION_FAILED",
        message="Registration failed: the SIP server hostname could not be resolved.",
        suggested_action="Check the configured host and DNS/network connectivity.",
        retryable=True,
    )
    assert diag_dns["error_code"] == "DNS_RESOLUTION_FAILED"
    assert diag_dns["status"] == "dns_error"
    assert diag_dns["retryable"] is True

    # 8. Network / Socket Timeout
    diag_net = make_sip_diagnostic_error(
        status="network_error",
        error_code="NETWORK_TIMEOUT",
        message="Registration timed out.",
        suggested_action="Verify that the SIP server address and port are reachable.",
        retryable=True,
    )
    assert diag_net["error_code"] == "NETWORK_TIMEOUT"
    assert diag_net["status"] == "network_error"
    assert diag_net["retryable"] is True


def test_sensitive_credentials_never_exposed():
    """Verify passwords, authorization headers, and digest responses are never exposed in diagnostics or API output."""
    raw_secret = "super_secret_carrier_password_xyz999"
    encrypted_pw = encrypt_sip_secret(raw_secret)

    acc = MagicMock()
    acc.id = "acc-privacy-1"
    acc.organization_id = "org-1"
    acc.name = "Privacy Trunk"
    acc.username = "user_sec"
    acc.auth_username = "auth_sec"
    acc.domain = "sip.privacy.com"
    acc.host = "sip.privacy.com"
    acc.port = 5060
    acc.transport = "udp"
    acc.outbound_proxy = None
    acc.caller_id = "+15550001"
    acc.direction = "both"
    acc.enabled = True
    acc.registration_status = "auth_failed"
    acc.last_health_status = "unhealthy"
    acc.last_health_latency_ms = 45.0
    acc.last_health_checked_at = None
    acc.last_error_message = "Registration failed: the SIP server rejected the authenticated REGISTER request (401 Unauthorized)."
    acc.max_channels = 10
    acc.password_hash = encrypted_pw
    acc.extension = None
    acc.dialing_prefix = None
    acc.display_name = "Privacy Trunk"
    acc.refresh_interval = 1800
    acc.keep_alive_interval = 20
    acc.local_ip = None
    acc.rtp_ip = None
    acc.rtp_port = None
    acc.stun_server = None
    acc.sip_ws_url = None
    acc.is_default = False
    acc.created_at = None
    acc.updated_at = None
    acc.settings = {
        "registration_diagnostics": {
            "status": "auth_failed",
            "error_code": "AUTH_REJECTED",
            "message": "Registration failed: the SIP server rejected the authenticated REGISTER request (401 Unauthorized). Verify the SIP authentication username and password, and confirm the provider's digest authentication requirements. The exact rejection cause has not been confirmed.",
            "sip_status_code": 401,
            "retryable": False,
            "suggested_action": "Verify credentials and digest requirements with VoIP provider.",
            "last_checked_at": "2026-10-22T10:00:00Z",
        }
    }

    api_out = _to_sip_account_out(acc, org_name="Security Org")

    # Raw password must not be present
    assert raw_secret not in str(api_out.dict())
    assert raw_secret not in (api_out.last_error_message or "")
    assert raw_secret not in (api_out.suggested_action or "")
    # Masked password must be bullets
    assert api_out.has_password is True
    assert api_out.masked_password == "••••••••"
    # Diagnostics must contain structured fields without secret leakage
    assert api_out.error_code == "AUTH_REJECTED"
    assert api_out.sip_status_code == 401
    assert api_out.retryable is False
    assert "Authorization" not in str(api_out.registration_diagnostics)
    assert "response=" not in str(api_out.registration_diagnostics)


@pytest.mark.asyncio
async def test_registration_isolation_by_account_and_organization():
    """Verify registration diagnostics and status updates remain isolated by account and organization."""
    from app.services.sip_account_service import register_and_update_sip_account

    acc1 = MagicMock()
    acc1.id = "acc-tenant1-1"
    acc1.organization_id = "tenant-alpha"
    acc1.name = "Alpha Account"
    acc1.host = "sip.alpha-carrier.com"
    acc1.port = 5060
    acc1.username = "alpha_user"
    acc1.auth_username = None
    acc1.password_hash = encrypt_sip_secret("alpha_pass")
    acc1.transport = "udp"
    acc1.registration_status = "unregistered"
    acc1.settings = {}
    acc1.last_error_message = None

    acc2 = MagicMock()
    acc2.id = "acc-tenant2-1"
    acc2.organization_id = "tenant-beta"
    acc2.name = "Beta Account"
    acc2.host = "sip.beta-carrier.com"
    acc2.port = 5060
    acc2.username = "beta_user"
    acc2.auth_username = None
    acc2.password_hash = encrypt_sip_secret("beta_pass")
    acc2.transport = "udp"
    acc2.registration_status = "registered"
    acc2.settings = {"registration_diagnostics": {"status": "registered", "sip_status_code": 200}}
    acc2.last_error_message = None

    mock_db = AsyncMock()

    # When testing acc1, mock a probe returning 403 Forbidden
    mock_probe_result = {
        "success": False,
        "status": "auth_failed",
        "error_code": "FORBIDDEN",
        "message": "Registration rejected: 403 Forbidden",
        "error": "SIP registration rejected: 403 Forbidden.",
        "sip_status_code": 403,
        "retryable": False,
        "suggested_action": "Check IP whitelisting with carrier.",
        "last_checked_at": "2026-10-22T10:30:00Z",
    }

    with patch("app.services.sip_account_service._perform_sip_register_probe", return_value=mock_probe_result):
        res1 = await register_and_update_sip_account(mock_db, acc1)

    # acc1 must be updated with the 403 Forbidden diagnostic
    assert res1["is_registered"] is False
    assert res1["error_code"] == "FORBIDDEN"
    assert res1["sip_status_code"] == 403
    assert acc1.registration_status == "auth_failed"
    assert acc1.settings["registration_diagnostics"]["error_code"] == "FORBIDDEN"

    # acc2 must remain completely untouched (still registered with 200 OK)
    assert acc2.registration_status == "registered"
    assert acc2.settings["registration_diagnostics"]["sip_status_code"] == 200
    assert acc2.last_error_message is None

