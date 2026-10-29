import socket
import uuid
import re
import hashlib

def run_test():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', 0))
    sock.settimeout(5.0)
    listen_port = sock.getsockname()[1]

    # Discover WAN IP
    import urllib.request
    try:
        contact_ip = urllib.request.urlopen('https://api.ipify.org', timeout=3).read().decode('utf-8').strip()
    except Exception:
        contact_ip = '49.205.80.130'

    target_dest_ip = '49.249.142.147'
    target_dest_port = 5060
    sip_host = '49.249.142.147'
    sip_port = 5060
    sip_user = '09410087540'
    sip_auth_user = '09410087540'
    sip_pass = '09410087540'
    caller_id = '917935485287'
    target_to = '9585310712'
    call_cid = f'test-{uuid.uuid4().hex[:8]}@callzenza'
    from_tag = uuid.uuid4().hex[:8]
    branch1 = f'z9hG4bK-{uuid.uuid4().hex[:8]}'

    from_hdr_val = f'"{caller_id}" <sip:{caller_id}@{sip_host}>;tag={from_tag}'
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

    print("Sending REQ 1...")
    sock.sendto(req1.encode('utf-8'), (target_dest_ip, target_dest_port))
    try:
        data, _ = sock.recvfrom(4096)
        resp1 = data.decode('utf-8', errors='ignore')
        print("=== RESPONSE 1 ===")
        print(resp1)
    except Exception as e:
        print("Failed to receive resp1:", e)
        return

    # Extract realm, nonce, etc.
    realm_m = re.search(r'realm="([^"]+)"', resp1)
    nonce_m = re.search(r'nonce="([^"]+)"', resp1)
    qop_m = re.search(r'qop="?([^",\r\n]+)"?', resp1)
    to_tag_m = re.search(r'To:.*?tag=([^\r\n;]+)', resp1, re.DOTALL)
    to_tag = to_tag_m.group(1) if to_tag_m else None

    # Crucial RFC 3261 step: Send ACK for the 401 response!
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
    print("Sending ACK for 401...")
    sock.sendto(ack1.encode('utf-8'), (target_dest_ip, target_dest_port))

    if not realm_m or not nonce_m:
        print("No realm/nonce in response 1")
        return

    realm = realm_m.group(1)
    nonce = nonce_m.group(1)
    qop = qop_m.group(1) if qop_m else None
    print(f"Realm: {realm}, Nonce: {nonce}, Qop: {qop}")

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

    print("Sending REQ 2 (Authenticated)...")
    sock.sendto(req2.encode('utf-8'), (target_dest_ip, target_dest_port))
    while True:
        try:
            data, addr = sock.recvfrom(4096)
            resp2 = data.decode('utf-8', errors='ignore')
            print(f"=== RECEIVED FROM {addr} ===")
            print(resp2)
            first_l = resp2.splitlines()[0]
            if "100" in first_l:
                print("Got 100 Trying, continuing to listen...")
                continue
            if "180" in first_l or "183" in first_l or "200" in first_l or "4" in first_l[:6] or "5" in first_l[:6]:
                break
        except Exception as e:
            print("Finished listening or timeout:", e)
            break

if __name__ == '__main__':
    run_test()
