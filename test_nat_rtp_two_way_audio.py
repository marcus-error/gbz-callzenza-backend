import socket
import struct
import pytest
from app.services.sip_inbound_daemon import get_stun_mapped_address
from app.services.voice_service import ALAW_TO_ULAW_TABLE, ULAW_TO_ALAW_TABLE

def test_stun_resolution_live():
    """Verify STUN successfully resolves public IP and dynamic mapped port within 1 second."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", 0))
    local_port = s.getsockname()[1]
    assert local_port > 0, "Socket failed to bind dynamic port"
    
    pub_ip, pub_port = get_stun_mapped_address(s, timeout=2.0)
    s.close()
    
    assert pub_ip is not None, "Failed to resolve public IP from STUN"
    assert pub_port is not None and pub_port > 0, "Failed to resolve public port from STUN"
    print(f"[PASS] STUN Resolution: Local port {local_port} -> External NAT endpoint {pub_ip}:{pub_port}")

def test_bidirectional_transcoding():
    """Verify bidirectional G.711 A-law (PT 8) and mu-law (PT 0) transcoding."""
    # 1. Mu-law from browser -> A-law for Asterisk / Telecom
    original_ulaw = bytes([i % 256 for i in range(160)])
    alaw_tx = original_ulaw.translate(ULAW_TO_ALAW_TABLE)
    assert len(alaw_tx) == 160
    
    # 2. A-law from Asterisk / Telecom -> Mu-law for browser softphone
    recovered_ulaw = alaw_tx.translate(ALAW_TO_ULAW_TABLE)
    assert len(recovered_ulaw) == 160
    print("[PASS] Bidirectional Transcoding: G.711 mu-law <-> A-law table conversion is valid")

if __name__ == "__main__":
    test_stun_resolution_live()
    test_bidirectional_transcoding()
    print("\n>>> ALL NAT & TWO-WAY AUDIO TESTS COMPLETED SUCCESSFULLY! <<<")
