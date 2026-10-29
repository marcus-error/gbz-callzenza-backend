import pytest
import re
from app.services.sip_inbound_daemon import (
    build_register_packet,
    compute_digest_auth,
    _extract_header_val,
)

def test_build_register_packet():
    packet = build_register_packet(
        host="49.249.142.147",
        port=5060,
        user="09410697548",
        contact_ip="192.168.0.37",
        listen_port=5080,
        call_id="reg-12345@callzenza",
        tag="tag1234",
        cseq_num=1
    )
    assert "REGISTER sip:49.249.142.147:5060 SIP/2.0" in packet
    assert "Via: SIP/2.0/UDP 192.168.0.37:5080;branch=" in packet
    assert "Contact: <sip:09410697548@192.168.0.37:5080>" in packet
    assert "Call-ID: reg-12345@callzenza" in packet
    assert "CSeq: 1 REGISTER" in packet

def test_authenticated_register_packet():
    auth_hdr = 'Digest username="09410697548", realm="asterisk", nonce="12345", uri="sip:49.249.142.147:5060", response="abcde", algorithm=MD5'
    packet = build_register_packet(
        host="49.249.142.147",
        port=5060,
        user="09410697548",
        contact_ip="192.168.0.37",
        listen_port=5080,
        call_id="reg-12345@callzenza",
        tag="tag1234",
        cseq_num=2,
        auth_header=auth_hdr
    )
    assert "CSeq: 2 REGISTER" in packet
    assert f"Authorization: {auth_hdr}" in packet

def test_digest_auth_computation():
    auth = compute_digest_auth(
        user="09410697548",
        pwd="secret_pass",
        realm="asterisk",
        nonce="5f3a2b1c",
        uri="sip:49.249.142.147:5060"
    )
    assert 'username="09410697548"' in auth
    assert 'realm="asterisk"' in auth
    assert 'nonce="5f3a2b1c"' in auth
    assert 'response=' in auth

def test_header_extraction():
    sip_msg = (
        "SIP/2.0 401 Unauthorized\r\n"
        "Via: SIP/2.0/UDP 192.168.0.37:5080;branch=z9hG4bK-abcdef12;rport\r\n"
        "From: <sip:09410697548@49.249.142.147>;tag=tag1234\r\n"
        "To: <sip:09410697548@49.249.142.147>;tag=as5678\r\n"
        "Call-ID: reg-12345@callzenza\r\n"
        "CSeq: 1 REGISTER\r\n"
        "Content-Length: 0\r\n\r\n"
    )
    assert _extract_header_val(sip_msg, "Call-ID") == "reg-12345@callzenza"
    assert _extract_header_val(sip_msg, "CSeq") == "1 REGISTER"
    via = _extract_header_val(sip_msg, "Via")
    branch = re.search(r'branch=([^;\s]+)', via).group(1)
    assert branch == "z9hG4bK-abcdef12"
