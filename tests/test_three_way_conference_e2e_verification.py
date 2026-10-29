import asyncio
import base64
import json
import uuid
from collections import deque
import pytest

from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    is_same_or_alias_leg,
    flush_barge_session_queues_for_transfer,
)


class MockWebSocket:
    """Mock WebSocket simulating browser agent telephony client."""
    def __init__(self, client_id: str):
        self.client_id = client_id
        self.sent_messages = []
        self.closed = False

    async def send_text(self, text: str):
        self.sent_messages.append(text)

    async def close(self, code: int = 1000):
        self.closed = True

    def get_received_media_payloads(self):
        payloads = []
        for m in self.sent_messages:
            try:
                data = json.loads(m)
                if data.get("event") == "media" and "media" in data:
                    b64 = data["media"].get("payload")
                    if b64:
                        payloads.append(base64.b64decode(b64))
            except Exception:
                pass
        return payloads


@pytest.mark.asyncio
async def test_3way_conference_alias_safety_no_customer_self_echo():
    """
    REGRESSION TEST FOR BUG: 'the voice is in loop'
    When a call has both a SIP Call-ID (e.g. 'call-primary@callzenza') and a db_call_id
    (e.g. 'call-uuid-1234') registered in session aliases, the mixer MUST NOT classify
    the db_call_id as a peer phone leg of the primary SIP leg.
    The customer's speech must NEVER be echoed back into rtp_leg_queues.
    """
    db_call_id = f"call-{uuid.uuid4().hex[:12]}"
    sip_call_id = f"call-{uuid.uuid4().hex[:12]}@callzenza"
    
    session = await _get_or_create_session(db_call_id)
    session["db_call_id"] = db_call_id
    session.setdefault("aliases", set()).update([db_call_id, sip_call_id, sip_call_id.split("@")[0]])
    barge_sessions[sip_call_id] = session

    # Customer transmits audio via primary leg
    customer_voice = b"\x35" * 160
    cust_in_leg = deque(maxlen=20)
    cust_in_leg.append(customer_voice)
    session["inbound_legs"][sip_call_id] = cust_in_leg

    # Set up RTP queues for both keys as could exist during call routing
    rtp_q_sip = deque(maxlen=50)
    rtp_q_db = deque(maxlen=50)
    session["rtp_leg_queues"][sip_call_id] = rtp_q_sip
    session["rtp_leg_queues"][db_call_id] = rtp_q_db

    # Verify alias helper identifies both as the same physical leg
    assert is_same_or_alias_leg(sip_call_id, db_call_id, session) is True
    assert is_same_or_alias_leg(sip_call_id, sip_call_id.split("@")[0], session) is True

    # Allow audio mixer loop to run ticks
    await asyncio.sleep(0.06)

    # CRITICAL ASSERTION: Neither queue must contain the customer's voice!
    # Zero customer self-echo
    for f in list(rtp_q_sip):
        assert f != customer_voice, "Customer audio was echoed back to SIP leg!"
    for f in list(rtp_q_db):
        assert f != customer_voice, "Customer audio was echoed back to DB call ID leg!"

    # Cleanup
    session["running"] = False
    barge_sessions.pop(db_call_id, None)
    barge_sessions.pop(sip_call_id, None)


@pytest.mark.asyncio
async def test_3way_conference_all_six_audio_paths_simultaneous():
    """
    COMPLETE END-TO-END 3-WAY CONFERENCE AUDIO VERIFICATION
    Verifies genuine simultaneous 3-party communication across:
    1. Customer (PSTN/SIP via RTP)
    2. Voice Agent 1 (Host Agent via WebSocket 1)
    3. Voice Agent 2 (Joined Agent via WebSocket 2)

    All 6 bidirectional audio paths tested:
    Path 1: Customer -> Voice Agent 1
    Path 2: Customer -> Voice Agent 2
    Path 3: Voice Agent 1 -> Customer
    Path 4: Voice Agent 2 -> Customer
    Path 5: Voice Agent 1 -> Voice Agent 2
    Path 6: Voice Agent 2 -> Voice Agent 1
    """
    call_id = f"conf-3way-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    session["db_call_id"] = call_id
    session["status"] = "CONFERENCE_3WAY_ACTIVE"

    # Register Voice Agent 1 and Voice Agent 2 WebSockets
    va1_ws = MockWebSocket("VA1_Host")
    va2_ws = MockWebSocket("VA2_Joined")
    session["browser_bargers"].add(va1_ws)
    session["browser_bargers"].add(va2_ws)

    va1_mic_q = deque(maxlen=20)
    va2_mic_q = deque(maxlen=20)
    session.setdefault("barger_in_queues", {})[va1_ws] = va1_mic_q
    session.setdefault("barger_in_queues", {})[va2_ws] = va2_mic_q

    # Customer RTP leg
    cust_rtp_leg_id = f"sip-cust-{uuid.uuid4().hex[:6]}@callzenza"
    session.setdefault("aliases", set()).update([call_id, cust_rtp_leg_id])
    cust_inbound_q = deque(maxlen=20)
    session["inbound_legs"][cust_rtp_leg_id] = cust_inbound_q

    cust_rtp_out_q = deque(maxlen=50)
    session["rtp_leg_queues"][cust_rtp_leg_id] = cust_rtp_out_q

    # Distinct test audio frames
    CUST_AUDIO = b"\x10" * 160  # Customer speech
    VA1_AUDIO = b"\x20" * 160   # Agent 1 speech
    VA2_AUDIO = b"\x30" * 160   # Agent 2 speech

    # =========================================================================
    # TEST STEP 1: Customer speaks. Both VA1 and VA2 must hear Customer.
    # (Path 1: Customer -> VA1, Path 2: Customer -> VA2)
    # Customer must NOT hear themselves (Zero self-echo).
    # =========================================================================
    cust_inbound_q.append(CUST_AUDIO)
    await asyncio.sleep(0.06)

    va1_payloads = va1_ws.get_received_media_payloads()
    va2_payloads = va2_ws.get_received_media_payloads()

    assert any(p == CUST_AUDIO for p in va1_payloads), "Path 1 FAIL: VA1 did not receive Customer audio"
    assert any(p == CUST_AUDIO for p in va2_payloads), "Path 2 FAIL: VA2 did not receive Customer audio"
    assert CUST_AUDIO not in list(cust_rtp_out_q), "Customer heard own voice (Self-echo violation)!"

    # Clear buffers for next step
    va1_ws.sent_messages.clear()
    va2_ws.sent_messages.clear()
    cust_rtp_out_q.clear()

    # =========================================================================
    # TEST STEP 2: Voice Agent 1 speaks. Customer and VA2 must hear VA1.
    # (Path 3: VA1 -> Customer, Path 5: VA1 -> VA2)
    # VA1 must NOT hear themselves (Zero self-echo).
    # =========================================================================
    va1_mic_q.append(VA1_AUDIO)
    await asyncio.sleep(0.06)

    va1_payloads = va1_ws.get_received_media_payloads()
    va2_payloads = va2_ws.get_received_media_payloads()
    cust_received = list(cust_rtp_out_q)

    assert any(p == VA1_AUDIO for p in cust_received), "Path 3 FAIL: Customer did not receive VA1 audio"
    assert any(p == VA1_AUDIO for p in va2_payloads), "Path 5 FAIL: VA2 did not receive VA1 audio"
    assert VA1_AUDIO not in va1_payloads, "VA1 received own voice (VA1 self-echo violation)!"

    va1_ws.sent_messages.clear()
    va2_ws.sent_messages.clear()
    cust_rtp_out_q.clear()

    # =========================================================================
    # TEST STEP 3: Voice Agent 2 speaks. Customer and VA1 must hear VA2.
    # (Path 4: VA2 -> Customer, Path 6: VA2 -> VA1)
    # VA2 must NOT hear themselves (Zero self-echo).
    # =========================================================================
    va2_mic_q.append(VA2_AUDIO)
    await asyncio.sleep(0.06)

    va1_payloads = va1_ws.get_received_media_payloads()
    va2_payloads = va2_ws.get_received_media_payloads()
    cust_received = list(cust_rtp_out_q)

    assert any(p == VA2_AUDIO for p in cust_received), "Path 4 FAIL: Customer did not receive VA2 audio"
    assert any(p == VA2_AUDIO for p in va1_payloads), "Path 6 FAIL: VA1 did not receive VA2 audio"
    assert VA2_AUDIO not in va2_payloads, "VA2 received own voice (VA2 self-echo violation)!"

    va1_ws.sent_messages.clear()
    va2_ws.sent_messages.clear()
    cust_rtp_out_q.clear()

    # =========================================================================
    # TEST STEP 4: Simultaneous 3-party speech:
    # Customer, VA1, and VA2 all speak at the exact same 20ms instant.
    # - Customer receives mixed (VA1 + VA2)
    # - VA1 receives mixed (Customer + VA2)
    # - VA2 receives mixed (Customer + VA1)
    # =========================================================================
    expected_cust_downlink = mix_ulaw_bytes(VA1_AUDIO, VA2_AUDIO)
    expected_va1_downlink = mix_ulaw_bytes(CUST_AUDIO, VA2_AUDIO)
    expected_va2_downlink = mix_ulaw_bytes(CUST_AUDIO, VA1_AUDIO)

    cust_inbound_q.append(CUST_AUDIO)
    va1_mic_q.append(VA1_AUDIO)
    va2_mic_q.append(VA2_AUDIO)
    await asyncio.sleep(0.06)

    va1_payloads = va1_ws.get_received_media_payloads()
    va2_payloads = va2_ws.get_received_media_payloads()
    cust_received = list(cust_rtp_out_q)

    assert any(p == expected_cust_downlink for p in cust_received), "Simultaneous: Customer did not receive mix(VA1 + VA2)"
    assert any(p == expected_va1_downlink for p in va1_payloads), "Simultaneous: VA1 did not receive mix(Customer + VA2)"
    assert any(p == expected_va2_downlink for p in va2_payloads), "Simultaneous: VA2 did not receive mix(Customer + VA1)"

    # =========================================================================
    # TEST STEP 5: Voice Agent 2 disconnects / leaves conference.
    # Voice Agent 1 and Customer must remain connected with seamless bidirectional audio.
    # =========================================================================
    session["browser_bargers"].remove(va2_ws)
    session["barger_in_queues"].pop(va2_ws, None)
    va1_ws.sent_messages.clear()
    cust_rtp_out_q.clear()

    # Customer speaks after VA2 leaves -> VA1 still receives audio
    cust_inbound_q.append(CUST_AUDIO)
    # VA1 speaks after VA2 leaves -> Customer still receives audio
    va1_mic_q.append(VA1_AUDIO)
    await asyncio.sleep(0.06)

    va1_payloads = va1_ws.get_received_media_payloads()
    cust_received = list(cust_rtp_out_q)

    assert any(p == CUST_AUDIO for p in va1_payloads), "Post-leave: VA1 did not receive Customer audio"
    assert any(p == VA1_AUDIO for p in cust_received), "Post-leave: Customer did not receive VA1 audio"

    # Cleanup
    session["running"] = False
    barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_inbound_queue_bounded_during_human_conference():
    """
    Verifies that when a human conference or barge session is active,
    inbound_queue does NOT grow unbounded and cause 4000-frame overflow warnings.
    """
    call_id = f"conf-queue-bound-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)
    session["is_voice_agent_call"] = True
    session["status"] = "CONFERENCE_3WAY_ACTIVE"

    # Ingest 120 customer frames into inbound_queue
    frame = b"\x7f" * 160
    for _ in range(120):
        session["inbound_queue"].put_nowait(frame)

    # Let audio_mixer_loop tick
    session["inbound_legs"]["test_leg"] = deque([frame], maxlen=10)
    await asyncio.sleep(0.08)

    # inbound_queue must be strictly bounded to <= 50 frames
    assert session["inbound_queue"].qsize() <= 50

    # Cleanup
    session["running"] = False
    barge_sessions.pop(call_id, None)
