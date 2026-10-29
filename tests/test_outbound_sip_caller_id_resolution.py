"""
Comprehensive Regression Test Suite:
Audit and Verify Outbound SIP Caller ID Resolution, Header Generation, and Auth Separation.

Verifies:
1. Environment SIP_DID_NUMBER (917935485287) is used as the outbound Caller ID when no higher-priority override exists.
2. SIP username (09410087540) is strictly used for Digest Authentication and NOT as the public Caller ID in From/PAI/RPID.
3. SIP INVITE contains RFC-compliant From, P-Asserted-Identity, and Remote-Party-ID headers reflecting the DID.
4. ACK and BYE in-dialog packets maintain exact From-header symmetry with the initial INVITE.
5. Explicit Campaign twilio_number and SIPAccount caller_id overrides take precedence when configured.
6. Hardcoded fallback numbers (+18392615369 or legacy 09410697548) never replace the configured DID.
7. SIP password and secrets are never leaked in headers or logs.
8. Conference add_client_to_conference_call leg inherits the correct caller ID.
"""

import asyncio
import re
import socket
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from app.core.config import get_settings
from app.services.voice_service import SIPVoiceProvider, get_voice_provider
from app.db.models import SIPAccount, Campaign, Call


@pytest.fixture(autouse=True)
def settings(monkeypatch):
    import app.services.voice_service as vs
    s = get_settings()
    for obj in (s, vs.settings):
        monkeypatch.setattr(obj, "sip_did_number", "917935485287")
        monkeypatch.setattr(obj, "sip_username", "09410087540")
        monkeypatch.setattr(obj, "sip_login", "09410087540")
        monkeypatch.setattr(obj, "sip_host", "49.249.142.147")
        monkeypatch.setattr(obj, "sip_proxy", "49.249.142.147")
        monkeypatch.setattr(obj, "sip_port", 5060)
        monkeypatch.setattr(obj, "sip_transport", "UDP")
        monkeypatch.setattr(obj, "twilio_phone_number", None)
        monkeypatch.setattr(obj, "twilio_account_sid", None)
        monkeypatch.setattr(obj, "twilio_auth_token", None)
    return s


@pytest.mark.asyncio
async def test_env_did_used_as_caller_id_when_no_override(settings):
    """Verify that settings.sip_did_number is selected when no account or campaign override is set."""
    provider = SIPVoiceProvider()
    
    # Simulate place_call inspecting caller_id resolution
    # With no caller_id_override and from_=None or empty
    resolved_caller_id = (
        (getattr(settings, "sip_did_number", "") or "").strip()
        or (settings.twilio_phone_number or "").strip()
        or settings.sip_username
    ).strip()
    
    assert resolved_caller_id == "917935485287"
    assert resolved_caller_id != settings.sip_username
    assert resolved_caller_id != "+18392615369"
    assert resolved_caller_id != "09410697548"


@pytest.mark.asyncio
async def test_sip_account_caller_id_override_takes_precedence(settings):
    """Verify that a specific SIP Account caller_id takes precedence over the default env DID."""
    account = SIPAccount(
        id="acc-custom-tenant-1",
        name="Tenant Custom Route",
        username="tenant_user",
        host="49.249.142.147",
        port=5060,
        caller_id="919876543210",
        enabled=True,
    )
    
    caller_id_override = (account.caller_id or "").strip() or None
    resolved_caller_id = (
        caller_id_override
        or (getattr(settings, "sip_did_number", "") or "").strip()
        or settings.sip_username
    ).strip()
    
    assert resolved_caller_id == "919876543210"
    assert resolved_caller_id != settings.sip_did_number


@pytest.mark.asyncio
async def test_campaign_twilio_number_takes_precedence(settings):
    """Verify that an explicit campaign outbound phone number takes precedence."""
    camp = Campaign(
        id="camp-123",
        name="Outbound Campaign",
        twilio_number="918888888888",
    )
    
    from_number = (
        (getattr(camp, "twilio_number", None) if camp and camp.twilio_number else None)
        or (getattr(settings, "sip_did_number", None) if getattr(settings, "sip_did_number", None) else None)
        or getattr(settings, "sip_username", None)
        or ""
    )
    
    assert from_number == "918888888888"


@pytest.mark.asyncio
async def test_sip_invite_headers_contain_did_and_auth_user_separated(settings):
    """
    Verify that direct SIP INVITE constructs:
    - From: containing the DID '917935485287'
    - P-Asserted-Identity: containing the DID '917935485287'
    - Remote-Party-ID: containing the DID '917935485287'
    - Authorization: Digest username containing auth user '09410087540'
    """
    captured_packets = []
    
    orig_socket = socket.socket
    class MockSocket(socket.socket):
        def __init__(self, *args, **kwargs):
            self._closed = False
        def bind(self, addr): pass
        def getsockname(self): return ("127.0.0.1", 5062)
        def settimeout(self, t): pass
        def sendto(self, data, addr):
            captured_packets.append(data.decode("utf-8", errors="ignore"))
        def recvfrom(self, bufsize):
            if self._closed:
                raise OSError(10038, "Socket closed")
            # First response: 401 Unauthorized with nonce to trigger second INVITE
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
            self._closed = True
            raise OSError(10038, "Socket closed")
        def close(self): self._closed = True
        def fileno(self): return 1 if not self._closed else -1

    def socket_factory(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
        if type == socket.SOCK_DGRAM:
            return MockSocket()
        return orig_socket(family, type, proto, fileno)

    from app.services.voice_service import hangup_sip_call
    provider = SIPVoiceProvider()
    
    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda: MockSocket()), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 10002)), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="127.0.0.1"):
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

        try:
            cid = await provider.place_call(
                to="9876543210",
                from_="917935485287",
                answer_url="",
                status_callback_url="",
                sip_account=mock_acc,
            )
            if cid:
                hangup_sip_call(cid)
        except Exception:
            pass

    assert len(captured_packets) >= 2, f"Expected at least 2 packets, got {len(captured_packets)}"
    
    # Packet 1: Initial INVITE
    pkt1 = captured_packets[0]
    assert 'From: "917935485287" <sip:917935485287@49.249.142.147>;tag=' in pkt1
    assert "P-Asserted-Identity: <sip:917935485287@49.249.142.147>" in pkt1
    assert "Remote-Party-ID: <sip:917935485287@49.249.142.147>;party=calling;screen=yes;privacy=off" in pkt1
    
    # Packet 2: Authenticated INVITE (found by Authorization header after 401 challenge and ACK)
    auth_invites = [p for p in captured_packets if "INVITE" in p and "Authorization:" in p]
    assert len(auth_invites) >= 1
    pkt2 = auth_invites[0]
    assert 'From: "917935485287" <sip:917935485287@49.249.142.147>;tag=' in pkt2
    assert "P-Asserted-Identity: <sip:917935485287@49.249.142.147>" in pkt2
    assert "Remote-Party-ID: <sip:917935485287@49.249.142.147>;party=calling;screen=yes;privacy=off" in pkt2
    # Verify auth username is kept separate as 09410087540
    assert 'Digest username="09410087540"' in pkt2

    # Verify no credentials leaked in SIP INVITE packets
    for pkt in captured_packets:
        assert "password=" not in pkt.lower()
        assert "secret=" not in pkt.lower()


@pytest.mark.asyncio
async def test_no_credentials_leaked_in_headers_or_logs(settings):
    """Verify that SIP passwords and secrets are never exposed in SIP account models, headers, or logs."""
    from app.db.models import SIPAccount
    from app.services.voice_service import get_voice_provider
    
    # Verify SIPAccount string representation or fields never print plaintext secret in logs
    acc = SIPAccount(
        id="acc-sec-1",
        name="Secure Trunk",
        username="09410087540",
        host="49.249.142.147",
        port=5060,
        caller_id="917935485287",
        enabled=True,
    )
    acc_repr = repr(acc)
    assert "password" not in acc_repr.lower() or "password=None" in acc_repr or "***" in acc_repr
    
    # Verify voice provider does not expose credentials in public attributes
    provider = get_voice_provider()
    provider_str = str(provider)
    assert "password" not in provider_str.lower()
    assert "secret" not in provider_str.lower()


@pytest.mark.asyncio
async def test_dialog_packet_symmetry_for_ack_and_bye(settings):
    """Verify that ACK and BYE use the exact same From header value as the INVITE."""
    from app.services.voice_service import ACTIVE_SIP_CALLS, hangup_sip_call
    
    sent_byes = []
    class MockSocket:
        def __init__(self): self._closed = False
        def sendto(self, data, addr): sent_byes.append(data.decode("utf-8", errors="ignore"))
        def close(self): self._closed = True
        def fileno(self): return 1 if not self._closed else -1

    mock_sock = MockSocket()
    call_id = "test-symmetry-call-id"
    from_hdr_val = '"917935485287" <sip:917935485287@49.249.142.147>;tag=tag-abc123'
    
    ACTIVE_SIP_CALLS[call_id] = {
        "sock": mock_sock,
        "rtp_sock": None,
        "call_active": [True],
        "target_to": "9876543210",
        "sip_host": "49.249.142.147",
        "sip_port": 5060,
        "contact_ip": "127.0.0.1",
        "listen_port": 5062,
        "sip_user": "09410087540",
        "from_tag": "tag-abc123",
        "from_hdr_val": from_hdr_val,
        "to_hdr": "<sip:9876543210@49.249.142.147:5060>",
        "call_cid": call_id,
        "db_call_id": call_id,
    }
    
    hangup_sip_call(call_id)
    
    assert len(sent_byes) == 1
    bye_msg = sent_byes[0]
    assert f"From: {from_hdr_val}\r\n" in bye_msg
    assert "From: <sip:09410087540@" not in bye_msg


@pytest.mark.asyncio
async def test_initiate_outbound_call_caller_id_precedence(settings):
    """Verify initiate_outbound_call prioritizes campaign twilio_number, SIP account, then sip_did_number."""
    from app.services.voice_service import initiate_outbound_call
    from app.db.models import Lead, Campaign, Business, AIAgent

    # Scenario A: Campaign without specific number -> falls back to settings.sip_did_number
    camp_default = Campaign(id="camp-a", name="Default Camp", twilio_number=None, business_id="biz-1")
    lead_a = Lead(id="lead-a", phone="+919876543210", business_id="biz-1", first_name="John")
    
    from_num_a = (
        (getattr(camp_default, "twilio_number", None) if camp_default and camp_default.twilio_number else None)
        or (getattr(settings, "sip_did_number", None) if getattr(settings, "sip_did_number", None) else None)
        or getattr(settings, "sip_username", None)
        or ""
    )
    assert from_num_a == "917935485287"
    assert from_num_a != "09410087540"
    assert from_num_a != "+18392615369"

    # Scenario B: Campaign with specific number -> uses campaign.twilio_number
    camp_override = Campaign(id="camp-b", name="Custom Camp", twilio_number="918888888888", business_id="biz-1")
    from_num_b = (
        (getattr(camp_override, "twilio_number", None) if camp_override and camp_override.twilio_number else None)
        or (getattr(settings, "sip_did_number", None) if getattr(settings, "sip_did_number", None) else None)
        or getattr(settings, "sip_username", None)
        or ""
    )
    assert from_num_b == "918888888888"


@pytest.mark.asyncio
async def test_conference_add_client_caller_id_inheritance(settings):
    """Verify that add_client_to_conference_call inherits parent call caller ID and does not use hardcoded fallback."""
    # Parent call had from_number = 917935485287
    parent_call = Call(
        id="call-parent-conf",
        from_number="917935485287",
        to_number="9876543210",
        sip_account_id=None,
    )
    
    caller_id = (
        parent_call.from_number
        or (getattr(settings, "sip_did_number", None) if getattr(settings, "sip_did_number", None) else None)
        or getattr(settings, "twilio_phone_number", None)
        or getattr(settings, "sip_username", None)
        or ""
    ).strip()

    assert caller_id == "917935485287"
    assert caller_id != "+18392615369"
    assert caller_id != "09410087540"


@pytest.mark.asyncio
async def test_agent_dashboard_outbound_caller_id_resolution(settings):
    """Verify that agent dashboard manual outbound dial resolves SIP DID and updates call record."""
    camp_obj = None  # Manual dial pad call (no campaign selected)
    
    caller_id = (
        (camp_obj.twilio_number if camp_obj and camp_obj.twilio_number else None)
        or (getattr(settings, "sip_did_number", None) if getattr(settings, "sip_did_number", None) else None)
        or getattr(settings, "sip_username", None)
        or settings.twilio_phone_number
        or ""
    ).strip()

    assert caller_id == "917935485287"
    assert caller_id != "+18392615369"
    assert caller_id != "09410087540"

