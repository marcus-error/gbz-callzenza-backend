import sys
sys.path.insert(0, '.')
import socket
import uuid
import re
import hashlib
import sqlite3
from app.services.sip_account_service import decrypt_sip_secret

def run_test():
    conn = sqlite3.connect('callzenza.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    row = c.execute("SELECT * FROM sip_accounts WHERE id = '5be4d78c-a8b6-4258-a089-9e159867fd23'").fetchone()
    conn.close()

    sip_host = row['host']
    sip_port = row['port']
    sip_user = row['username']
    sip_auth_user = row['auth_username'] or row['username']
    sip_pass = decrypt_sip_secret(row['password_hash'])
    caller_id = row['caller_id']
    target_to = '9585310712'

    print(f"Testing Asterisk at {sip_host}:{sip_port} with user={sip_user}, auth_user={sip_auth_user}, caller_id={caller_id}")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', 0))
    sock.settimeout(5.0)
    listen_port = sock.getsockname()[1]
    contact_ip = '49.205.80.130'

    call_cid = f'test-{uuid.uuid4().hex[:8]}@callzenza'
    from_tag = uuid.uuid4().hex[:8]
    branch1 = f'z9hG4bK-{uuid.uuid4().hex[:8]}'

    # Test 1: From URI using sip_user vs caller_id
    from_hdr_val = f'"{caller_id}" <sip:{sip_user}@{sip_host}>;tag={from_tag}'
    req1 = (
        f"INVITE sip:{target_to}@{sip_host}:{sip_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch={branch1};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: {from_hdr_val}\r\n"
        f"To: <sip:{target_to}@{sip_host}:{sip_port}>\r\n"
        f"Call-ID: {call_cid}\r\n"
        f"CSeq: 1 INVITE\r\n"
        f"Contact: <sip:{sip_user}@{contact_ip}:{listen_port}>\r\n"
        f"Content-Length: 0\r\n\r\n"
    )

    sock.sendto(req1.encode('utf-8'), (sip_host, sip_port))
    try:
        data, _ = sock.recvfrom(4096)
        resp1 = data.decode('utf-8', errors='ignore')
        first_line = resp1.splitlines()[0]
        print("Response 1:", first_line)
    except Exception as e:
        print("Timeout on req1:", e)
        return

    realm_m = re.search(r'realm="([^"]+)"', resp1)
    nonce_m = re.search(r'nonce="([^"]+)"', resp1)
    qop_m = re.search(r'qop="?([^",\r\n]+)"?', resp1)
    to_tag_m = re.search(r'To:.*?tag=([^\r\n;]+)', resp1, re.DOTALL)
    to_tag = to_tag_m.group(1) if to_tag_m else None

    # ACK for 401
    to_hdr_ack = f"<sip:{target_to}@{sip_host}:{sip_port}>"
    if to_tag:
        to_hdr_ack += f";tag={to_tag}"
    ack1 = (
        f"ACK sip:{target_to}@{sip_host}:{sip_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch={branch1};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: {from_hdr_val}\r\n"
        f"To: {to_hdr_ack}\r\n"
        f"Call-ID: {call_cid}\r\n"
        f"CSeq: 1 ACK\r\n"
        f"Content-Length: 0\r\n\r\n"
    )
    sock.sendto(ack1.encode('utf-8'), (sip_host, sip_port))

    realm = realm_m.group(1)
    nonce = nonce_m.group(1)
    qop = qop_m.group(1) if qop_m else None

    uri = f"sip:{target_to}@{sip_host}:{sip_port}"
    ha1 = hashlib.md5(f"{sip_auth_user}:{realm}:{sip_pass}".encode("utf-8")).hexdigest()
    ha2 = hashlib.md5(f"INVITE:{uri}".encode("utf-8")).hexdigest()
    nc = "00000001"
    cnonce = uuid.uuid4().hex[:8]

    if qop and "auth" in qop:
        resp_hash = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode("utf-8")).hexdigest()
        auth = f'Digest username="{sip_auth_user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp_hash}", algorithm=MD5, cnonce="{cnonce}", qop=auth, nc={nc}'
    else:
        resp_hash = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()
        auth = f'Digest username="{sip_auth_user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp_hash}", algorithm=MD5'

    sdp = (
        "v=0\r\n"
        f"o={sip_user} 1000 1000 IN IP4 {contact_ip}\r\n"
        "s=CallZenza\r\n"
        f"c=IN IP4 {contact_ip}\r\n"
        "t=0 0\r\n"
        f"m=audio 10008 RTP/AVP 0 8 101\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
        "a=rtpmap:8 PCMA/8000\r\n"
        "a=rtpmap:101 telephone-event/8000\r\n"
        "a=fmtp:101 0-16\r\n"
        "a=ptime:20\r\n"
        "a=sendrecv\r\n"
    )

    branch2 = f"z9hG4bK-{uuid.uuid4().hex[:8]}"
    req2 = (
        f"INVITE sip:{target_to}@{sip_host}:{sip_port} SIP/2.0\r\n"
        f"Via: SIP/2.0/UDP {contact_ip}:{listen_port};branch={branch2};rport\r\n"
        f"Max-Forwards: 70\r\n"
        f"From: {from_hdr_val}\r\n"
        f"To: <sip:{target_to}@{sip_host}:{sip_port}>\r\n"
        f"Call-ID: {call_cid}\r\n"
        f"CSeq: 2 INVITE\r\n"
        f"Contact: <sip:{sip_user}@{contact_ip}:{listen_port}>\r\n"
        f"Authorization: {auth}\r\n"
        f"Content-Type: application/sdp\r\n"
        f"Content-Length: {len(sdp)}\r\n\r\n"
        f"{sdp}"
    )

    print("Sending REQ 2 (Authenticated with sip_user in From URI)...")
    sock.sendto(req2.encode('utf-8'), (sip_host, sip_port))
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            resp2 = data.decode('utf-8', errors='ignore')
            lines = resp2.splitlines()
            first_l = lines[0] if lines else ""
            print("Response 2 line:", first_l)
            if "100" in first_l:
                continue
            for l in lines:
                if any(k in l.lower() for k in ["reason", "warning", "server", "cause"]):
                    print("  ->", l)
            break
        except Exception as e:
            print("Finished listening or timeout:", e)
            break

if __name__ == '__main__':
    run_test()
