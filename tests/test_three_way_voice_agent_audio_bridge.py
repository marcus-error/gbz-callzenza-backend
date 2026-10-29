import asyncio
import base64
import json
import uuid
import pytest

from app.api.routes.barge_stream import (
    barge_sessions,
    _get_or_create_session,
    mix_ulaw_bytes,
    audio_mixer_loop,
)


class MockWebSocket:
    """Mock WebSocket that records all text messages sent to it."""
    def __init__(self, name: str):
        self.name = name
        self.sent_messages = []
        self.closed = False
        self.close_code = None

    async def send_text(self, text: str):
        if self.closed:
            raise RuntimeError(f"Cannot send to closed WebSocket {self.name}")
        self.sent_messages.append(text)

    async def close(self, code: int = 1000):
        self.closed = True
        self.close_code = code


@pytest.mark.asyncio
async def test_three_way_audio_mixer_all_directions_and_overlap():
    """
    TEST A, B, C, D: Full end-to-end verification of 3-way conference mixer loop.
    - Voice Agent 1 speaks -> Agent 2 & Client hear speech.
    - Voice Agent 2 speaks -> Agent 1 & Client hear speech.
    - Client speaks -> Agent 1 & Agent 2 hear speech.
    - Overlapping speech: all 3 speak simultaneously -> mixed cleanly without muting or self-echo.
    """
    call_id = f"test-3way-mixer-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    # Attach Twilio / SIP customer stream
    cust_ws = MockWebSocket("Client")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-3way"

    # Attach Voice Agent 1 (Softphone browser)
    ws_agent1 = MockWebSocket("Agent1")
    session["browser_bargers"].add(ws_agent1)

    # Attach Voice Agent 2 (Softphone browser)
    ws_agent2 = MockWebSocket("Agent2")
    session["browser_bargers"].add(ws_agent2)

    # Distinct 160-byte 20ms G.711 mu-law audio frames
    frame_client = b"\x10" * 160
    frame_agent1 = b"\x20" * 160
    frame_agent2 = b"\x30" * 160

    # Ensure mixer loop is running
    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # -------------------------------------------------------------
        # STEP 1: Voice Agent 1 speaks alone
        # -------------------------------------------------------------
        b_q1 = session.setdefault("barger_in_queues", {}).setdefault(ws_agent1, asyncio.Queue())
        # Use deque as defined in production
        from collections import deque
        if not isinstance(session["barger_in_queues"][ws_agent1], deque):
            session["barger_in_queues"][ws_agent1] = deque(maxlen=5)
        session["barger_in_queues"][ws_agent1].append(frame_agent1)

        await asyncio.sleep(0.045)  # Allow 2 ticks (40ms)

        # Agent 2 must receive Agent 1's audio
        assert len(ws_agent2.sent_messages) >= 1
        last_agent2_msg = json.loads(ws_agent2.sent_messages[-1])
        assert base64.b64decode(last_agent2_msg["media"]["payload"]) == frame_agent1

        # Client must receive Agent 1's audio
        assert len(cust_ws.sent_messages) >= 1
        last_cust_msg = json.loads(cust_ws.sent_messages[-1])
        assert base64.b64decode(last_cust_msg["media"]["payload"]) == frame_agent1

        # Agent 1 must NOT receive self-echo
        assert len(ws_agent1.sent_messages) == 0

        # Clear recorded messages for next step
        ws_agent1.sent_messages.clear()
        ws_agent2.sent_messages.clear()
        cust_ws.sent_messages.clear()

        # -------------------------------------------------------------
        # STEP 2: Voice Agent 2 speaks alone
        # -------------------------------------------------------------
        if not isinstance(session["barger_in_queues"].get(ws_agent2), deque):
            session["barger_in_queues"][ws_agent2] = deque(maxlen=5)
        session["barger_in_queues"][ws_agent2].append(frame_agent2)

        await asyncio.sleep(0.045)

        # Agent 1 must receive Agent 2's audio
        assert len(ws_agent1.sent_messages) >= 1
        last_agent1_msg = json.loads(ws_agent1.sent_messages[-1])
        assert base64.b64decode(last_agent1_msg["media"]["payload"]) == frame_agent2

        # Client must receive Agent 2's audio
        assert len(cust_ws.sent_messages) >= 1
        last_cust_msg = json.loads(cust_ws.sent_messages[-1])
        assert base64.b64decode(last_cust_msg["media"]["payload"]) == frame_agent2

        # Agent 2 must NOT receive self-echo
        assert len(ws_agent2.sent_messages) == 0

        ws_agent1.sent_messages.clear()
        ws_agent2.sent_messages.clear()
        cust_ws.sent_messages.clear()

        # -------------------------------------------------------------
        # STEP 3: Client speaks alone
        # -------------------------------------------------------------
        barge_in_q = session.get("barge_inbound_queue")
        await barge_in_q.put(frame_client)

        await asyncio.sleep(0.045)

        # Both Agent 1 and Agent 2 must receive Client's audio
        assert len(ws_agent1.sent_messages) >= 1
        assert len(ws_agent2.sent_messages) >= 1
        rec1 = json.loads(ws_agent1.sent_messages[-1])
        rec2 = json.loads(ws_agent2.sent_messages[-1])
        assert base64.b64decode(rec1["media"]["payload"]) == frame_client
        assert base64.b64decode(rec2["media"]["payload"]) == frame_client

        # Client must NOT receive self-echo
        assert len(cust_ws.sent_messages) == 0

        ws_agent1.sent_messages.clear()
        ws_agent2.sent_messages.clear()
        cust_ws.sent_messages.clear()

        # -------------------------------------------------------------
        # STEP 4: Simultaneous Overlapping Speech (All 3 Speak at Once!)
        # -------------------------------------------------------------
        await barge_in_q.put(frame_client)
        session["barger_in_queues"][ws_agent1].append(frame_agent1)
        session["barger_in_queues"][ws_agent2].append(frame_agent2)

        await asyncio.sleep(0.045)

        # Client must receive mixed Agent 1 + Agent 2
        expected_client_mix = mix_ulaw_bytes(frame_agent1, frame_agent2)
        assert len(cust_ws.sent_messages) >= 1
        rec_client = json.loads(cust_ws.sent_messages[-1])
        assert base64.b64decode(rec_client["media"]["payload"]) == expected_client_mix

        # Agent 1 must receive mixed Client + Agent 2 (NO self-echo of Agent 1!)
        expected_agent1_mix = mix_ulaw_bytes(frame_client, frame_agent2)
        assert len(ws_agent1.sent_messages) >= 1
        rec_agent1 = json.loads(ws_agent1.sent_messages[-1])
        assert base64.b64decode(rec_agent1["media"]["payload"]) == expected_agent1_mix

        # Agent 2 must receive mixed Client + Agent 1 (NO self-echo of Agent 2!)
        expected_agent2_mix = mix_ulaw_bytes(frame_client, frame_agent1)
        assert len(ws_agent2.sent_messages) >= 1
        rec_agent2 = json.loads(ws_agent2.sent_messages[-1])
        assert base64.b64decode(rec_agent2["media"]["payload"]) == expected_agent2_mix

        # -------------------------------------------------------------
        # STEP 5: Call Recording Captures All 3 Audio Tracks Mixed
        # -------------------------------------------------------------
        recorded_frames = session.get("recorded_frames", [])
        assert len(recorded_frames) > 0
        expected_full_composite = mix_ulaw_bytes(frame_client, expected_client_mix)
        assert any(f == expected_full_composite for f in recorded_frames)

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)


@pytest.mark.asyncio
async def test_agent1_disconnects_client_and_agent2_remain_active():
    """
    TEST E, F: Verifies that if Agent 1 disconnects (e.g. transfer completion or hangup),
    Client and Agent 2 remain fully connected in active two-way conversation.
    """
    call_id = f"test-agent1-leaves-{uuid.uuid4().hex[:8]}"
    session = await _get_or_create_session(call_id)

    cust_ws = MockWebSocket("Client")
    session["customer_ws"] = cust_ws
    session["customer_stream_sid"] = "cust-stream-leaves"

    ws_agent1 = MockWebSocket("Agent1")
    ws_agent2 = MockWebSocket("Agent2")
    session["browser_bargers"].add(ws_agent1)
    session["browser_bargers"].add(ws_agent2)

    mixer_task = asyncio.create_task(audio_mixer_loop(call_id))

    try:
        # Agent 1 disconnects
        session["browser_bargers"].discard(ws_agent1)
        session.get("barger_in_queues", {}).pop(ws_agent1, None)
        await ws_agent1.close(1000)

        # Agent 2 speaks to Client
        agent2_frame = b"\x7a" * 160
        from collections import deque
        if not isinstance(session["barger_in_queues"].get(ws_agent2), deque):
            session["barger_in_queues"][ws_agent2] = deque(maxlen=5)
        session["barger_in_queues"][ws_agent2].append(agent2_frame)

        await asyncio.sleep(0.045)

        # Client receives Agent 2's frame
        assert len(cust_ws.sent_messages) >= 1
        rec_cust = json.loads(cust_ws.sent_messages[-1])
        assert base64.b64decode(rec_cust["media"]["payload"]) == agent2_frame

        # Client speaks to Agent 2
        cust_frame = b"\x8b" * 160
        barge_in_q = session.get("barge_inbound_queue")
        await barge_in_q.put(cust_frame)

        await asyncio.sleep(0.045)

        # Agent 2 receives Client's frame
        assert len(ws_agent2.sent_messages) >= 1
        rec_agent2 = json.loads(ws_agent2.sent_messages[-1])
        assert base64.b64decode(rec_agent2["media"]["payload"]) == cust_frame

        # Agent 1 is disconnected and receives nothing
        assert len(ws_agent1.sent_messages) == 0

    finally:
        session["running"] = False
        mixer_task.cancel()
        try:
            await mixer_task
        except asyncio.CancelledError:
            pass
        barge_sessions.pop(call_id, None)
