import socket
import pytest
from app.services.voice_service import hangup_sip_call, ACTIVE_SIP_CALLS

class MockSocket:
    def __init__(self):
        self.closed = False
        self.sent_bytes = []
        self.fd = 100

    def fileno(self):
        if self.closed:
            return -1
        return self.fd

    def sendto(self, data, addr):
        if self.closed:
            raise OSError(10038, "An operation was attempted on something that is not a socket")
        self.sent_bytes.append((data, addr))

    def close(self):
        if self.closed:
            raise OSError(10038, "An operation was attempted on something that is not a socket")
        self.closed = True
        self.fd = -1


def create_mock_call_record(call_cid="call-cid-123", db_call_id="db-call-id-456"):
    sock = MockSocket()
    rtp_sock = MockSocket()
    info = {
        "sock": sock,
        "rtp_sock": rtp_sock,
        "call_active": [True],
        "target_to": "1001",
        "sip_host": "127.0.0.1",
        "sip_port": 5060,
        "contact_ip": "127.0.0.1",
        "listen_port": 5060,
        "sip_user": "agent",
        "from_tag": "tag123",
        "to_hdr": "<sip:1001@127.0.0.1>",
        "call_cid": call_cid,
        "db_call_id": db_call_id,
        "cseq": 10,
    }
    ACTIVE_SIP_CALLS[db_call_id] = info
    ACTIVE_SIP_CALLS[call_cid] = info
    return info, sock, rtp_sock


def test_scenario_1_normal_call_hangup():
    db_id = "test-call-norm-1"
    cid = "test-cid-norm-1"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    res = hangup_sip_call(db_id)
    assert res is True
    assert sock.closed is True
    assert rtp_sock.closed is True
    assert len(sock.sent_bytes) == 1
    assert b"BYE sip:1001@127.0.0.1:5060" in sock.sent_bytes[0][0]
    assert db_id not in ACTIVE_SIP_CALLS
    assert cid not in ACTIVE_SIP_CALLS


def test_scenario_2_customer_hangs_up_first():
    db_id = "test-call-cust-2"
    cid = "test-cid-cust-2"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    # Remote side closes socket first
    info["sock_closed"] = True
    sock.close()

    res = hangup_sip_call(db_id)
    assert res is True
    assert db_id not in ACTIVE_SIP_CALLS
    assert cid not in ACTIVE_SIP_CALLS


def test_scenario_3_ai_initiates_hangup():
    db_id = "test-call-ai-3"
    cid = "test-cid-ai-3"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    res = hangup_sip_call(cid)
    assert res is True
    assert sock.closed is True
    assert rtp_sock.closed is True


def test_scenario_4_websocket_disconnect_followed_by_sip_cleanup():
    db_id = "test-call-ws-4"
    cid = "test-cid-ws-4"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    # Step A: WebSocket disconnect calls hangup
    res1 = hangup_sip_call(db_id)
    assert res1 is True
    assert sock.closed is True

    # Step B: Follow-up SIP cleanup call
    res2 = hangup_sip_call(cid)
    assert res2 is True


def test_scenario_5_sip_cleanup_followed_by_websocket_disconnect():
    db_id = "test-call-sip-5"
    cid = "test-cid-sip-5"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    # Step A: SIP cleanup calls hangup
    res1 = hangup_sip_call(cid)
    assert res1 is True
    assert sock.closed is True

    # Step B: WebSocket disconnect calls hangup
    res2 = hangup_sip_call(db_id)
    assert res2 is True


def test_scenario_6_duplicate_hangup_invocation():
    db_id = "test-call-dup-6"
    cid = "test-cid-dup-6"
    info, sock, rtp_sock = create_mock_call_record(cid, db_id)

    # Repeated calls to hangup_sip_call back-to-back
    res1 = hangup_sip_call(db_id)
    res2 = hangup_sip_call(db_id)
    res3 = hangup_sip_call(cid)
    res4 = hangup_sip_call(cid)

    assert res1 is True
    assert res2 is True
    assert res3 is True
    assert res4 is True
    assert sock.closed is True
    assert rtp_sock.closed is True
    # Ensure BYE packet was only dispatched ONCE despite 4 hangup calls
    assert len(sock.sent_bytes) == 1
