import pytest
import re

def test_destination_number_sanitization_and_masking():
    # Test number masking logic
    def mask_phone(phone_str: str) -> str:
        if not phone_str:
            return ""
        digits = "".join(c for c in str(phone_str) if c.isdigit())
        if len(digits) <= 4:
            return "****"
        return f"{digits[:2]}****{digits[-3:]}"

    raw_input = "+91 9410087540"
    digits_only = "".join(c for c in raw_input if c.isdigit())
    if digits_only.startswith("91") and len(digits_only) >= 12:
        digits_only = digits_only[2:]
    
    prefix = "0"
    final_target = f"{prefix}{digits_only}" if prefix else digits_only

    assert digits_only == "9410087540"
    assert final_target == "09410087540"
    assert mask_phone(raw_input) == "91****540"
    assert mask_phone(final_target) == "09****540"

def test_q850_cause_code_extraction():
    sip_response = (
        "SIP/2.0 480 Temporarily Unavailable\r\n"
        "Via: SIP/2.0/UDP 192.168.0.100:5062;branch=z9hG4bK-12345;rport\r\n"
        "From: <sip:user@host>;tag=tag1\r\n"
        "To: <sip:dest@host>;tag=tag2\r\n"
        "Call-ID: test-call-id-123\r\n"
        "CSeq: 2 INVITE\r\n"
        'Reason: Q.850;cause=18;text="No user responding"\r\n'
        "Content-Length: 0\r\n\r\n"
    )

    reason_hdr = ""
    q850_cause = None
    q850_text = ""
    for line in sip_response.splitlines():
        if line.lower().startswith("reason:"):
            reason_hdr = line.split(":", 1)[1].strip()
            cause_m = re.search(r'cause=(\d+)', reason_hdr, re.IGNORECASE)
            if cause_m:
                q850_cause = cause_m.group(1)
            text_m = re.search(r'text="([^"]+)"', reason_hdr, re.IGNORECASE)
            if text_m:
                q850_text = text_m.group(1)

    assert q850_cause == "18"
    assert q850_text == "No user responding"

def test_duplicate_udp_response_signature_deduplication():
    first_line = "SIP/2.0 480 Temporarily Unavailable"
    call_cid = "call-12345@callzenza"
    
    last_resp_sig = None
    last_resp_time = 0.0

    processed_count = 0

    # Simulate 2 UDP packets arriving at timestamps t=10.0 and t=10.01
    packets = [
        (first_line, call_cid, 10.00),
        (first_line, call_cid, 10.01),
    ]

    for line, cid, now_t in packets:
        resp_sig = f"{line}:{cid}"
        if resp_sig == last_resp_sig and (now_t - last_resp_time) < 0.5:
            continue  # Duplicate skipped
        last_resp_sig = resp_sig
        last_resp_time = now_t
        processed_count += 1

    assert processed_count == 1
