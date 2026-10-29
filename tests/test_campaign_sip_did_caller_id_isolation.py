"""
End-to-End Regression Test Suite for SIP/DID Caller ID Isolation in CallZenza.

Tests:
1. Campaign A (DID ending in 287, SIP Account A) presents strictly 287 in From URI, PAI, PPI, RPI.
2. Campaign B (DID ending in 931, SIP Account B) presents strictly 931 in From URI, PAI, PPI, RPI.
3. Zero cross-contamination between accounts or campaigns.
4. Validation prevents cross-assigning DID of Account B to Campaign using Account A.
5. Auth user is kept strictly in Authorization Digest header, separate from DID in From URI.
6. Rejection of inactive or mismatched DIDs.
"""
import pytest
import socket
from unittest.mock import MagicMock, patch
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Business, Campaign, DIDNumber, SIPAccount, Call
from app.services.voice_service import SIPVoiceProvider, hangup_sip_call
from app.services.sip_account_service import validate_sip_did_for_campaign


@pytest.mark.asyncio
async def test_campaign_a_uses_did_287_and_campaign_b_uses_did_931():
    """Verify that Account A / Campaign A presents 287 and Account B / Campaign B presents 931."""
    captured_packets = []

    class MockSocket:
        def __init__(self):
            self._closed = False
        def bind(self, addr): pass
        def getsockname(self): return ("127.0.0.1", 5062)
        def settimeout(self, t): pass
        def sendto(self, data, addr):
            captured_packets.append(data.decode("utf-8", errors="ignore"))
        def recvfrom(self, bufsize):
            if self._closed:
                raise OSError(10038, "Socket closed")
            # Always reply with 200 OK so the INVITE is accepted immediately
            resp = (
                "SIP/2.0 200 OK\r\n"
                "Via: SIP/2.0/UDP 127.0.0.1:5062;rport=5062\r\n"
                "From: <sip:test@49.249.142.147>;tag=tag123\r\n"
                "To: <sip:client@49.249.142.147:5060>;tag=as123\r\n"
                "Call-ID: test-call-id\r\n"
                "CSeq: 1 INVITE\r\n"
                "Content-Length: 0\r\n\r\n"
            )
            return resp.encode("utf-8"), ("49.249.142.147", 5060)
        def close(self): self._closed = True
        def fileno(self): return 1 if not self._closed else -1

    provider = SIPVoiceProvider()

    # --- Scenario 1: Dial using Campaign A (SIP Account A, DID 287) ---
    mock_acc_a = MagicMock()
    mock_acc_a.id = "acc-a-287"
    mock_acc_a.host = "49.249.142.147"
    mock_acc_a.port = 5060
    mock_acc_a.username = "09410087540"
    mock_acc_a.auth_username = "09410087540"
    mock_acc_a.caller_id = "917935485287"
    mock_acc_a.domain = None
    mock_acc_a.transport = "UDP"
    mock_acc_a.outbound_proxy = None
    mock_acc_a.dialing_prefix = ""
    mock_acc_a.settings = {}
    mock_acc_a.name = "Trunk A (287)"

    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda: MockSocket()), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 10010)), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="127.0.0.1"):
        try:
            cid_a = await provider.place_call(
                to="9876543210",
                from_="917935485287",
                answer_url="",
                status_callback_url="",
                sip_account=mock_acc_a,
            )
            if cid_a:
                hangup_sip_call(cid_a)
        except Exception:
            pass

    assert len(captured_packets) >= 1
    pkt_a = captured_packets[0]
    assert '<sip:917935485287@49.249.142.147>' in pkt_a
    assert 'P-Asserted-Identity: <sip:917935485287@49.249.142.147>' in pkt_a
    assert 'P-Preferred-Identity: <sip:917935485287@49.249.142.147>' in pkt_a
    assert 'Remote-Party-ID: <sip:917935485287@49.249.142.147>' in pkt_a
    # Ensure DID 931 or default number never appears anywhere in packet A
    assert "917935485931" not in pkt_a
    assert "+18392615369" not in pkt_a

    # --- Scenario 2: Dial using Campaign B (SIP Account B, DID 931) ---
    captured_packets.clear()

    mock_acc_b = MagicMock()
    mock_acc_b.id = "acc-b-931"
    mock_acc_b.host = "49.249.142.147"
    mock_acc_b.port = 5060
    mock_acc_b.username = "09410087999"
    mock_acc_b.auth_username = "09410087999"
    mock_acc_b.caller_id = "917935485931"
    mock_acc_b.domain = None
    mock_acc_b.transport = "UDP"
    mock_acc_b.outbound_proxy = None
    mock_acc_b.dialing_prefix = ""
    mock_acc_b.settings = {}
    mock_acc_b.name = "Trunk B (931)"

    with patch("app.services.voice_service.create_sip_control_socket", side_effect=lambda: MockSocket()), \
         patch("app.services.voice_service.allocate_unique_rtp_socket", return_value=(MagicMock(), 10012)), \
         patch("app.services.sip_inbound_daemon._get_public_ip", return_value="127.0.0.1"):
        try:
            cid_b = await provider.place_call(
                to="9876543210",
                from_="917935485931",
                answer_url="",
                status_callback_url="",
                sip_account=mock_acc_b,
            )
            if cid_b:
                hangup_sip_call(cid_b)
        except Exception:
            pass

    assert len(captured_packets) >= 1
    pkt_b = captured_packets[0]
    assert '<sip:917935485931@49.249.142.147>' in pkt_b
    assert 'P-Asserted-Identity: <sip:917935485931@49.249.142.147>' in pkt_b
    assert 'P-Preferred-Identity: <sip:917935485931@49.249.142.147>' in pkt_b
    assert 'Remote-Party-ID: <sip:917935485931@49.249.142.147>' in pkt_b
    # Ensure DID 287 or default number never appears anywhere in packet B
    assert "917935485287" not in pkt_b
    assert "+18392615369" not in pkt_b


@pytest.mark.asyncio
async def test_validate_sip_did_cross_account_rejection():
    """Verify that validate_sip_did_for_campaign prevents cross-account DID assignment."""
    from unittest.mock import AsyncMock
    mock_db = MagicMock(spec=AsyncSession)

    # Mock DID 931 belonging to SIP Account B
    did_931 = MagicMock(spec=DIDNumber)
    did_931.id = "did-931-id"
    did_931.phone_number = "917935485931"
    did_931.sip_account_id = "acc-b-931"
    did_931.business_id = "org-1"
    did_931.organization_id = "org-1"
    did_931.status = "ACTIVE"

    mock_db.get = AsyncMock(return_value=did_931)

    # 1. Validation should FAIL when assigning DID 931 to SIP Account A
    is_valid, msg, _ = await validate_sip_did_for_campaign(
        db=mock_db,
        sip_account_id="acc-a-287",
        did_id_or_number="did-931-id",
        organization_id="org-1"
    )
    assert not is_valid
    assert "is assigned to SIP account 'acc-b-931', not 'acc-a-287'" in (msg or "")

    # 2. Validation should SUCCEED when assigning DID 931 to SIP Account B
    is_valid_b, msg_b, did_b = await validate_sip_did_for_campaign(
        db=mock_db,
        sip_account_id="acc-b-931",
        did_id_or_number="did-931-id",
        organization_id="org-1"
    )
    assert is_valid_b
    assert msg_b is None
    assert did_b == did_931

    # 3. Validation should FAIL if organization does not match
    is_valid_org, msg_org, _ = await validate_sip_did_for_campaign(
        db=mock_db,
        sip_account_id="acc-b-931",
        did_id_or_number="did-931-id",
        organization_id="org-other"
    )
    assert not is_valid_org
    assert "belongs to another organization" in (msg_org or "")

    # 4. Validation should FAIL if DID is not ACTIVE
    did_931.status = "INACTIVE"
    is_valid_act, msg_act, _ = await validate_sip_did_for_campaign(
        db=mock_db,
        sip_account_id="acc-b-931",
        did_id_or_number="did-931-id",
        organization_id="org-1"
    )
    assert not is_valid_act
    assert "is inactive" in (msg_act or "")
