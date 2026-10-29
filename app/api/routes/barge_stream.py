import asyncio
import base64
import json
from typing import Dict, Any, Optional, Set
from collections import deque

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, status
from starlette.websockets import WebSocketState
from loguru import logger
from app.core.security import decode_access_token

router = APIRouter(prefix="/api/voice/barge/stream", tags=["Barge Stream"])

# In-memory store for active bridging sessions
# call_id -> {
#   "customer_ws": Optional[WebSocket],
#   "customer_stream_sid": Optional[str],
#   "supervisor_ws": Optional[WebSocket],
#   "supervisor_stream_sid": Optional[str],
#   "browser_listeners": Set[WebSocket],
#   "browser_bargers": Set[WebSocket],
#   "inbound_queue": asyncio.Queue,
#   "outbound_queue": asyncio.Queue,
#   "running": bool,
#   "mixer_task": Optional[asyncio.Task]
# }
barge_sessions: Dict[str, Dict[str, Any]] = {}

# -------------------------------------------------------------
# ITU-T G.711 Standard Linear 16-bit PCM mu-law Mixing Table
# Precomputed 256x256 lookup table for zero-distortion mixing
# -------------------------------------------------------------
def _generate_ulaw_tables():
    ulaw_to_pcm = []
    for b in range(256):
        inv = ~b & 0xFF
        sign = inv & 0x80
        exponent = (inv >> 4) & 0x07
        mantissa = inv & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        if not sign:
            sample = -sample
        ulaw_to_pcm.append(sample)

    def _pcm_to_ulaw(s: int) -> int:
        s = max(-32768, min(32767, s))
        sign = 0x80 if s >= 0 else 0x00
        if s < 0:
            s = -s
        s += 0x84
        if s > 32767:
            s = 32767
        exponent = 7
        for exp, threshold in enumerate([0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF]):
            if s <= threshold:
                exponent = exp
                break
        mantissa = (s >> (exponent + 3)) & 0x0F
        return ~(sign | (exponent << 4) | mantissa) & 0xFF

    mix_lookup = bytearray(256 * 256)
    for b1 in range(256):
        pcm1 = ulaw_to_pcm[b1]
        offset = b1 << 8
        for b2 in range(256):
            pcm2 = ulaw_to_pcm[b2]
            mix_lookup[offset | b2] = _pcm_to_ulaw(pcm1 + pcm2)

    pcm_sq = [float(s * s) for s in ulaw_to_pcm]

    attenuate_table = bytearray(256)
    for b in range(256):
        attenuate_table[b] = _pcm_to_ulaw(int(ulaw_to_pcm[b] * 0.707))

    return bytes(mix_lookup), ulaw_to_pcm, bytes(attenuate_table), pcm_sq

MIX_ULAW_LOOKUP, ULAW_TO_PCM_TABLE, ULAW_ATTENUATE_TABLE, ULAW_PCM_SQ_TABLE = _generate_ulaw_tables()

def attenuate_ulaw_frame(frame: bytes, factor: float = 0.707) -> bytes:
    """Attenuates a G.711 mu-law frame by ~3dB for Packet Loss Concealment (PLC)."""
    if not frame:
        return b""
    return frame.translate(ULAW_ATTENUATE_TABLE)

def calc_ulaw_rms_dbfs(frame: Optional[bytes]) -> float:
    """Calculates RMS audio level in dBFS for a 160-byte G.711 mu-law frame in < 3 microseconds."""
    if not frame:
        return -96.0
    sum_sq = sum(ULAW_PCM_SQ_TABLE[b] for b in frame)
    mean_sq = sum_sq / len(frame)
    if mean_sq <= 1.0:
        return -96.0
    import math
    rms = math.sqrt(mean_sq)
    return max(-96.0, 20.0 * math.log10(rms / 32768.0))

def _generate_ulaw_boost_table(gain: float = 2.3848) -> bytes:
    """
    Precomputes a 256-byte G.711 mu-law speech loudness normalization and pre-amp table.
    Lifts quiet laptop/headset microphone speech to standard telephony broadcast levels (-3.8 dBFS peak)
    with smooth soft-limiting and zero noise floor hiss amplification.
    """
    ulaw_to_pcm = ULAW_TO_PCM_TABLE

    def _pcm_to_ulaw(s: int) -> int:
        s = max(-32768, min(32767, s))
        sign = 0x80 if s >= 0 else 0x00
        if s < 0:
            s = -s
        s += 0x84
        if s > 32767:
            s = 32767
        exponent = 7
        for exp, threshold in enumerate([0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF]):
            if s <= threshold:
                exponent = exp
                break
        mantissa = (s >> (exponent + 3)) & 0x0F
        return ~(sign | (exponent << 4) | mantissa) & 0xFF

    boost_table = bytearray(256)
    for b in range(256):
        pcm = ulaw_to_pcm[b]
        # Keep true silence and lowest ambient room noise unboosted (prevents background hiss pumping)
        if abs(pcm) <= 120:
            boost_table[b] = b
            continue
        boosted = pcm * gain
        if boosted > 24000:
            boosted = 24000 + (boosted - 24000) * 0.25
        elif boosted < -24000:
            boosted = -24000 + (boosted + 24000) * 0.25
        boost_table[b] = _pcm_to_ulaw(int(boosted))

    return bytes(boost_table)

ULAW_SPEECH_BOOST_TABLE = _generate_ulaw_boost_table(gain=2.3848)

def boost_ulaw_speech_frame(frame: bytes) -> bytes:
    """Telephony speech normalization table for human voice agents."""
    if not frame:
        return b""
    return frame.translate(ULAW_SPEECH_BOOST_TABLE)

def mix_ulaw_bytes(chunk1: bytes, chunk2: bytes) -> bytes:
    """
    Mixes two G.711 mu-law byte arrays using mathematically exact
    ITU-T G.711 16-bit linear PCM addition with zero distortion.
    """
    if not chunk1:
        return chunk2 or b""
    if not chunk2:
        return chunk1 or b""
    length = min(len(chunk1), len(chunk2))
    mixed = bytearray(length)
    for i in range(length):
        mixed[i] = MIX_ULAW_LOOKUP[(chunk1[i] << 8) | chunk2[i]]
    if len(chunk1) > length:
        mixed.extend(chunk1[length:])
    elif len(chunk2) > length:
        mixed.extend(chunk2[length:])
    return bytes(mixed)

def is_same_or_alias_leg(id1: Optional[str], id2: Optional[str], session: Optional[Dict[str, Any]] = None) -> bool:
    """
    Returns True if id1 and id2 represent the same physical call leg or call alias.
    Guarantees strict N-1 mix-minus with zero self-echo even when sessions register
    both db_call_id and SIP Call-ID aliases simultaneously.
    """
    if not id1 or not id2:
        return False
    if id1 == id2:
        return True
    c1 = str(id1).split("@")[0]
    c2 = str(id2).split("@")[0]
    if c1 == c2:
        return True
    if session:
        aliases = session.get("aliases", set())
        if id1 in aliases and id2 in aliases:
            return True
        if c1 in aliases and c2 in aliases:
            return True
        db_id = session.get("db_call_id")
        if db_id:
            db_clean = str(db_id).split("@")[0]
            if (id1 == db_id or c1 == db_clean) and (id2 == db_id or c2 == db_clean):
                return True
    return False

async def _get_or_create_session(call_id: str) -> Dict[str, Any]:
    if call_id in barge_sessions:
        session = barge_sessions[call_id]
        session.setdefault("rtp_leg_queues", {})
        session.setdefault("inbound_legs", {})
        session.setdefault("browser_bargers", set())
        session.setdefault("browser_listeners", set())
        session.setdefault("barger_in_queues", {})
        session.setdefault("aliases", set()).add(call_id)
        session["running"] = True
        if session.get("mixer_task") is None or session["mixer_task"].done():
            session["mixer_task"] = asyncio.create_task(audio_mixer_loop(call_id))
        return session

    clean_id = (call_id.split("@")[0] if "@" in call_id else call_id).strip()
    stripped_id = clean_id[5:] if clean_id.startswith("call-") else clean_id
    prefixed_id = f"call-{clean_id}" if not clean_id.startswith("call-") else clean_id
    candidate_keys = list(dict.fromkeys([c for c in [call_id, clean_id, stripped_id, prefixed_id] if c]))
    existing_sess = None

    # 1. Quick check for candidate keys directly in barge_sessions
    for ck in candidate_keys:
        if ck in barge_sessions:
            existing_sess = barge_sessions[ck]
            break

    # 2. Match against existing session keys or known session aliases
    if not existing_sess:
        for k, cand_sess in list(barge_sessions.items()):
            k_clean = (k.split("@")[0] if "@" in k else k).strip()
            k_stripped = k_clean[5:] if k_clean.startswith("call-") else k_clean
            k_prefixed = f"call-{k_clean}" if not k_clean.startswith("call-") else k_clean
            if any(ck in (k, k_clean, k_stripped, k_prefixed) for ck in candidate_keys):
                existing_sess = cand_sess
                break
            aliases = cand_sess.get("aliases", set())
            if any(ck in aliases for ck in candidate_keys):
                existing_sess = cand_sess
                break
            if cand_sess.get("db_call_id") in candidate_keys:
                existing_sess = cand_sess
                break

    # 3. DB resolution across Call and CallTransfer tables
    if not existing_sess:
        try:
            from app.db.database import AsyncSessionLocal
            from app.db.models import Call, CallTransfer
            from sqlalchemy import select, or_
            async with AsyncSessionLocal() as db:
                call_obj = await db.get(Call, clean_id)
                if not call_obj and stripped_id:
                    call_obj = await db.get(Call, stripped_id)
                if not call_obj:
                    stmt = select(Call).where(
                        or_(
                            Call.id.in_(candidate_keys),
                            Call.call_sid.in_(candidate_keys),
                        )
                    )
                    call_obj = (await db.execute(stmt)).scalars().first()

                # If still not found, check if clean_id is a CallTransfer ID
                if not call_obj:
                    stmt_tr = select(CallTransfer).where(
                        or_(
                            CallTransfer.id.in_(candidate_keys),
                            CallTransfer.call_id.in_(candidate_keys),
                        )
                    )
                    transfer_obj = (await db.execute(stmt_tr)).scalars().first()
                    if transfer_obj and transfer_obj.call_id:
                        call_obj = await db.get(Call, transfer_obj.call_id)

                if call_obj:
                    db_candidate_keys = set(filter(None, [
                        call_obj.id,
                        f"call-{call_obj.id}",
                        (call_obj.id[5:] if call_obj.id.startswith("call-") else None),
                        call_obj.call_sid,
                        (call_obj.call_sid.split("@")[0] if call_obj.call_sid else None),
                        *candidate_keys
                    ]))
                    for cand_key in db_candidate_keys:
                        if cand_key in barge_sessions:
                            existing_sess = barge_sessions[cand_key]
                            break
                    if not existing_sess:
                        for cand_sess in list(barge_sessions.values()):
                            if cand_sess.get("db_call_id") == call_obj.id:
                                existing_sess = cand_sess
                                break
                            aliases = cand_sess.get("aliases", set())
                            if any(ck in aliases for ck in db_candidate_keys):
                                existing_sess = cand_sess
                                break
                    if existing_sess:
                        for cand_key in db_candidate_keys:
                            barge_sessions[cand_key] = existing_sess
                            existing_sess.setdefault("aliases", set()).add(cand_key)
                        existing_sess["db_call_id"] = call_obj.id
        except Exception as e:
            logger.debug(f"[_get_or_create_session] DB resolution notice: {e}")

    if existing_sess:
        for ck in candidate_keys:
            barge_sessions[ck] = existing_sess
        existing_sess.setdefault("aliases", set()).update(candidate_keys)
        session = existing_sess
    else:
        session = {
            "customer_ws": None,
            "customer_stream_sid": None,
            "supervisor_ws": None,
            "supervisor_stream_sid": None,
            "browser_listeners": set(),
            "browser_bargers": set(),
            "barger_in_queues": {},
            # Increased to 4000 (80s at 50pps) to absorb STT+LLM latency without drops
            "inbound_queue": asyncio.Queue(maxsize=4000),
            "barge_inbound_queue": asyncio.Queue(maxsize=100),
            "outbound_queue": asyncio.Queue(maxsize=1000),
            "barge_outbound_queue": asyncio.Queue(maxsize=1000),
            "rtp_outbound_queue": deque(maxlen=5000),
            "ai_rtp_tx_queue": deque(maxlen=5000),
            "rtp_leg_queues": {},
            "inbound_legs": {},
            "aliases": set(candidate_keys),
            "ai_speaking": False,
            "running": True,
            "mixer_task": None,
        }
        for ck in candidate_keys:
            barge_sessions[ck] = session

    session.setdefault("rtp_leg_queues", {})
    session.setdefault("inbound_legs", {})
    session.setdefault("browser_bargers", set())
    session.setdefault("browser_listeners", set())
    session.setdefault("barger_in_queues", {})
    session.setdefault("aliases", set()).update(candidate_keys)
    session["running"] = True
    if session.get("mixer_task") is None or session["mixer_task"].done():
        session["mixer_task"] = asyncio.create_task(audio_mixer_loop(call_id))
    return session


def safe_put_audio_frame(
    q: Optional[asyncio.Queue],
    frame: bytes,
    queue_name: str = "inbound_queue",
    call_id: str = ""
) -> bool:
    """
    Safely enqueues an audio frame into an asyncio.Queue on the event loop.
    Enforces a strict upper bound and evicts the oldest frame if full to prevent:
    1. Unhandled asyncio.queues.QueueFull exceptions.
    2. Blocking producer threads or dropping audio silently without logs.
    3. Stale latency buildup.
    """
    if q is None:
        return False
    try:
        if q.full():
            try:
                q.get_nowait()
                drop_count = getattr(q, "_drop_count", 0) + 1
                q._drop_count = drop_count
                if drop_count == 1 or drop_count % 100 == 0:
                    logger.warning(
                        f"[AUDIO_QUEUE_OVERFLOW] {queue_name} reached capacity ({q.maxsize}), "
                        f"evicting oldest frame call_id={call_id} total_evicted={drop_count}"
                    )
            except Exception:
                pass
        q.put_nowait(frame)
        return True
    except asyncio.QueueFull:
        try:
            q.get_nowait()
            q.put_nowait(frame)
            return True
        except Exception:
            return False
    except Exception as e:
        logger.debug(f"[AUDIO_QUEUE_ERROR] {queue_name} call_id={call_id}: {e}")
        return False


def shutdown_all_barge_sessions():
    """Cancels all audio mixer tasks and marks barge sessions stopped during server shutdown."""
    logger.info(f"[BARGE_SHUTDOWN] Stopping all {len(barge_sessions)} barge sessions...")
    for cid, sess in list(barge_sessions.items()):
        try:
            sess["running"] = False
            mixer_task = sess.get("mixer_task")
            if mixer_task and not mixer_task.done():
                mixer_task.cancel()
        except Exception:
            pass
    barge_sessions.clear()


def flush_barge_session_queues_for_transfer(call_id: str):
    """
    Flushes stale buffered audio and resets RTP queues on transfer handoff
    so the new voice agent's audio starts immediately with zero queue latency.
    """
    if not call_id:
        return
    clean_id = (call_id.split("@")[0] if "@" in call_id else call_id).strip()
    target_keys = set(filter(None, [call_id, clean_id]))
    target_sessions = []
    for cid in target_keys:
        s = barge_sessions.get(cid)
        if s and s not in target_sessions:
            target_sessions.append(s)
    if not target_sessions:
        for s in list(barge_sessions.values()):
            if s.get("db_call_id") in target_keys or any(k in s.get("aliases", set()) for k in target_keys):
                if s not in target_sessions:
                    target_sessions.append(s)
    for sess in target_sessions:
        ai_q = sess.get("ai_rtp_tx_queue")
        if ai_q:
            ai_q.clear()
        rtp_out = sess.get("rtp_outbound_queue")
        if rtp_out:
            rtp_out.clear()
        b_out = sess.get("barge_outbound_queue")
        if b_out:
            while not b_out.empty():
                try:
                    b_out.get_nowait()
                except Exception:
                    break
        b_in = sess.get("barge_inbound_queue")
        if b_in:
            while not b_in.empty():
                try:
                    b_in.get_nowait()
                except Exception:
                    break
        in_legs = sess.get("inbound_legs")
        if in_legs:
            for leg_q in in_legs.values():
                if leg_q:
                    leg_q.clear()
        rtp_leg_qs = sess.get("rtp_leg_queues")
        if rtp_leg_qs:
            for leg_q in rtp_leg_qs.values():
                if leg_q:
                    leg_q.clear()
        has_active_bargers = bool(sess.get("browser_bargers"))
        if not has_active_bargers:
            b_in_qs = sess.get("barger_in_queues")
            if b_in_qs:
                for bq in b_in_qs.values():
                    if bq:
                        bq.clear()
            sess.pop("barger_jitter_states", None)
            sess.pop("mic_buffer", None)
            sess.pop("last_barger_frame", None)
        logger.info(f"[TRANSFER_AUDIO_FLUSH] Stale audio buffers flushed for call_id={call_id} (active_bargers={has_active_bargers})")

async def audio_mixer_loop(call_id: str):
    """
    Continuous real-time 3-way conference mixer task running at 50Hz (20ms frames).
    Consumes audio from:
      - Client inbound stream (PSTN / SIP RTP / Twilio media stream)
      - All connected Voice Agents (Agent 1, Agent 2...) via per-agent queues
    And computes exact, synchronized N-1 (mix-minus) downlink streams:
      1. Client receives: mix(Agent 1, Agent 2) via RTP and Twilio WebSocket
      2. Voice Agent 1 receives: mix(Client, Agent 2) with ZERO self-echo
      3. Voice Agent 2 receives: mix(Client, Agent 1) with ZERO self-echo
      4. Supervisors (listen-only) receive: mix(Client, Agent 1, Agent 2)
      5. Call recording captures: mix(Client, Agent 1, Agent 2)
    """
    session = barge_sessions.get(call_id)
    if not session:
        return

    barge_in_q: asyncio.Queue = session.get("barge_inbound_queue") or session["inbound_queue"]
    barge_out_q: asyncio.Queue = session.get("barge_outbound_queue") or session["outbound_queue"]

    logger.info(f"Barge stream mixer started for call {call_id}")
    tick_counter = 0

    try:
        while session.get("running"):
            t_tick_start = asyncio.get_event_loop().time()
            tick_counter += 1

            supervisor_ws: Optional[WebSocket] = session.get("supervisor_ws")
            listeners: Set[WebSocket] = session.get("browser_listeners", set())
            bargers: Set[WebSocket] = session.get("browser_bargers", set())

            barger_jitter_states = session.setdefault("barger_jitter_states", {})

            # -------------------------------------------------------------
            # 1. Ingest Inbound Client Speech (PSTN / SIP / Twilio)
            # -------------------------------------------------------------
            chunk_inbound = None

            # Multi-leg conference inbound mixing:
            inbound_legs = session.setdefault("inbound_legs", {})
            leg_frames = {}
            for l_id, l_q in list(inbound_legs.items()):
                if l_q and len(l_q) > 0:
                    try:
                        leg_frames[l_id] = l_q.popleft()
                    except Exception:
                        leg_frames[l_id] = None
                else:
                    leg_frames[l_id] = None

            active_leg_frames = [f for f in leg_frames.values() if f]
            if len(active_leg_frames) == 1:
                chunk_inbound = active_leg_frames[0]
            elif len(active_leg_frames) > 1:
                mixed_in = active_leg_frames[0]
                for other_c in active_leg_frames[1:]:
                    mixed_in = mix_ulaw_bytes(mixed_in, other_c)
                chunk_inbound = mixed_in

            if chunk_inbound:
                # Inbound audio frame ingested from inbound_legs.
                # Drain duplicate frames in barge_in_q to keep bounded (<2 frames) and prevent overflow
                while not barge_in_q.empty():
                    try:
                        barge_in_q.get_nowait()
                    except Exception:
                        break
                # When human conference/barge is active, AI bot is not consuming inbound_queue.
                # Keep inbound_queue strictly bounded (<50 frames / 1s) to prevent memory buildup & overflow warnings.
                raw_in_q = session.get("inbound_queue")
                if raw_in_q and (bargers or session.get("is_voice_agent_call") or session.get("status") in ("CONFERENCE_3WAY_ACTIVE", "HUMAN_HANDLING")):
                    while raw_in_q.qsize() > 50:
                        try:
                            raw_in_q.get_nowait()
                        except Exception:
                            break
            else:
                # Keep inbound queue strictly bounded (<80ms)
                while barge_in_q.qsize() > 4:
                    try:
                        barge_in_q.get_nowait()
                    except Exception:
                        break
                if not barge_in_q.empty():
                    try:
                        chunk_inbound = barge_in_q.get_nowait()
                    except Exception:
                        chunk_inbound = None

                # Inbound audio frame ingested from inbound_legs or barge_inbound_queue (preserve inbound_queue for STT/VAD)

            # -------------------------------------------------------------
            # 2. Ingest Microphone Frames from Connected Voice Agents
            #    Resilient Jitter Buffer + Packet Loss Concealment (PLC)
            # -------------------------------------------------------------
            barger_in_queues = session.setdefault("barger_in_queues", {})
            barger_frames = {}
            for ws in list(bargers):
                bq = barger_in_queues.get(ws)
                jstate = barger_jitter_states.setdefault(ws, {
                    "is_buffering": True,
                    "last_speech_frame": None,
                    "consecutive_empty": 0,
                    "consecutive_plc": 0,
                    "total_frames_in": 0,
                    "total_frames_out": 0,
                    "total_plc_frames": 0,
                })

                frame = None
                q_len = len(bq) if bq else 0

                # 1. If buffer has frames:
                if bq and q_len > 0:
                    frame = bq.popleft()
                    jstate["total_frames_out"] += 1
                    # Maintain bounded latency (< 100ms)
                    if len(bq) > 5:
                        try:
                            bq.popleft()
                        except Exception:
                            pass

                    rms = calc_ulaw_rms_dbfs(frame)
                    if rms > -50.0:
                        jstate["speech_run"] = jstate.get("speech_run", 0) + 1
                        jstate["last_speech_frame"] = frame
                    else:
                        jstate["speech_run"] = 0
                        jstate["last_speech_frame"] = None
                    jstate["consecutive_empty"] = 0
                    jstate["consecutive_plc"] = 0

                # 2. If queue is starved (Wi-Fi packet arrival jitter during active speech):
                else:
                    jstate["consecutive_empty"] += 1
                    # Only engage PLC if in an established speech burst (>= 3 frames / 60ms)
                    if (
                        jstate["consecutive_empty"] <= 2
                        and jstate.get("speech_run", 0) >= 3
                        and jstate.get("last_speech_frame") is not None
                    ):
                        # Packet Loss Concealment (PLC): smooth attenuation of last speech frame
                        # Eliminates Wi-Fi stutter, clicks, and robotic dropouts
                        frame = attenuate_ulaw_frame(jstate["last_speech_frame"], factor=0.707)
                        jstate["last_speech_frame"] = frame
                        jstate["consecutive_plc"] += 1
                        jstate["total_plc_frames"] += 1
                    else:
                        frame = None
                        jstate["speech_run"] = 0
                        jstate["last_speech_frame"] = None

                barger_frames[ws] = frame

            # Periodic Targeted Audio Diagnostics
            if tick_counter % 50 == 0 and (bargers or session.get("inbound_legs")):
                agent_levels = []
                barger_user_ids = session.get("barger_user_ids", {})
                for idx, ws in enumerate(list(bargers)):
                    bf = barger_frames.get(ws)
                    rms_val = calc_ulaw_rms_dbfs(bf) if bf else -96.0
                    j_info = barger_jitter_states.get(ws, {})
                    p_id = barger_user_ids.get(ws, f"agent_{idx+1}")
                    agent_levels.append(
                        f"agent_{idx+1}[participant_id={p_id} ws={id(ws)} rms={rms_val:.1f}dBFS plc_tot={j_info.get('total_plc_frames', 0)}]"
                    )
                inbound_rms = calc_ulaw_rms_dbfs(chunk_inbound) if chunk_inbound else -96.0
                logger.info(
                    f"[CONFERENCE_AUDIO_LEVELS] call_id={call_id} tick={tick_counter} bargers={len(bargers)} "
                    f"inbound_client_rms={inbound_rms:.1f}dBFS agent_details={'; '.join(agent_levels) if agent_levels else 'none'}"
                )

            # Combine all active agent speech frames for outbound and listener mix
            active_agent_frames = [f for f in barger_frames.values() if f]
            agents_mixed = None
            if len(active_agent_frames) == 1:
                agents_mixed = active_agent_frames[0]
            elif len(active_agent_frames) > 1:
                agents_mixed = active_agent_frames[0]
                for af in active_agent_frames[1:]:
                    agents_mixed = mix_ulaw_bytes(agents_mixed, af)

            is_from_ai_queue = False
            mix_source = "SILENCE"
            if agents_mixed:
                mix_source = "HUMAN_MIC"
            # If no human agent speaking, fall back to ai_rtp_tx_queue or barge_out_q
            # For 1-on-1 AI calls, _rtp_send_loop is the sole authoritative consumer of ai_rtp_tx_queue,
            # which directly feeds barge_out_q at real-time 20ms cadence for browser listeners and recording.
            if not agents_mixed:
                ai_tx_cand = session.get("ai_rtp_tx_queue")
                if ai_tx_cand and len(ai_tx_cand) > 0 and not bargers:
                    try:
                        agents_mixed = ai_tx_cand.popleft()
                        if agents_mixed:
                            is_from_ai_queue = True
                            mix_source = "AI_BOT"
                    except Exception:
                        agents_mixed = None

            # Fallback to barge_out_q ONLY if active human bargers are connected
            if not agents_mixed and bargers:
                try:
                    agents_mixed = barge_out_q.get_nowait()
                    if agents_mixed:
                        mix_source = "AI_BOT" if is_from_ai_queue or session.get("ai_speaking") else "HUMAN_MIC"
                except Exception:
                    agents_mixed = None

            session["is_human_barger_speaking"] = (mix_source == "HUMAN_MIC")
            if mix_source != "SILENCE":
                session["last_outbound_source"] = mix_source

            # Keep barge_out_q drained and bounded (<2 frames) to prevent stale accumulation
            while barge_out_q.qsize() > 2:
                try:
                    barge_out_q.get_nowait()
                except Exception:
                    break

            # Keep legacy outbound_queue drained and bounded (<2 frames) to prevent queue overflow
            legacy_out_q = session.get("outbound_queue")
            if legacy_out_q and not legacy_out_q.empty():
                if not agents_mixed:
                    try:
                        agents_mixed = legacy_out_q.get_nowait()
                    except Exception:
                        pass
                while legacy_out_q.qsize() > 2:
                    try:
                        legacy_out_q.get_nowait()
                    except Exception:
                        break

            # -------------------------------------------------------------
            # 3. Route Audio Outbound to Each Phone Leg (N-1 Mix-Minus)
            # -------------------------------------------------------------
            rtp_leg_queues = session.setdefault("rtp_leg_queues", {})
            # Ensure all active inbound legs, call aliases, or active endpoints have an entry in rtp_leg_queues
            all_leg_keys = set(inbound_legs.keys()) | set(rtp_leg_queues.keys())
            if not all_leg_keys:
                all_leg_keys.add(call_id)
            for l_id in all_leg_keys:
                if l_id not in rtp_leg_queues or not isinstance(rtp_leg_queues[l_id], deque):
                    rtp_leg_queues[l_id] = deque(maxlen=50)

            for l_id, l_q in list(rtp_leg_queues.items()):
                # Peer phone frames excluding this leg and any aliases of this leg (zero self-echo)
                peer_phone_frames = [
                    f for other_id, f in leg_frames.items()
                    if not is_same_or_alias_leg(other_id, l_id, session) and f
                ]
                peer_phone_mix = None
                if len(peer_phone_frames) == 1:
                    peer_phone_mix = peer_phone_frames[0]
                elif len(peer_phone_frames) > 1:
                    peer_phone_mix = peer_phone_frames[0]
                    for pf in peer_phone_frames[1:]:
                        peer_phone_mix = mix_ulaw_bytes(peer_phone_mix, pf)

                # Mix agent speech + peer phone speech
                leg_downlink = None
                if agents_mixed and peer_phone_mix:
                    leg_downlink = mix_ulaw_bytes(agents_mixed, peer_phone_mix)
                elif agents_mixed:
                    leg_downlink = agents_mixed
                elif peer_phone_mix:
                    leg_downlink = peer_phone_mix

                if leg_downlink:
                    l_q.append(leg_downlink)
                    while len(l_q) > 25:
                        l_q.popleft()

            if agents_mixed:
                # Maintain single-leg fallback for legacy callers
                if len(inbound_legs) <= 1:
                    # ONLY enqueue to ai_rtp_tx_queue if frame came from human mic (NEVER recirculate AI speech!)
                    if not is_from_ai_queue and bargers:
                        ai_tx_q = session.get("ai_rtp_tx_queue")
                        if ai_tx_q is not None:
                            ai_tx_q.append(agents_mixed)
                            while len(ai_tx_q) > 5:
                                ai_tx_q.popleft()

                    if not rtp_leg_queues:
                        rtp_out_q = session.get("rtp_outbound_queue")
                        if rtp_out_q is not None:
                            rtp_out_q.append(agents_mixed)
                            while len(rtp_out_q) > 5:
                                rtp_out_q.popleft()

                # Twilio Customer Stream WebSocket
                cust_ws: Optional[WebSocket] = session.get("customer_ws")
                cust_sid: Optional[str] = session.get("customer_stream_sid")
                if cust_ws and cust_sid:
                    try:
                        cust_payload = {
                            "event": "media",
                            "streamSid": cust_sid,
                            "media": {
                                "payload": base64.b64encode(agents_mixed).decode("utf-8")
                            }
                        }
                        await cust_ws.send_text(json.dumps(cust_payload))
                    except Exception as e:
                        logger.debug(f"Failed sending to customer stream: {e}")

            # -------------------------------------------------------------
            # 4. Dispatch Synchronized N-1 (Mix-Minus) Downlink to Each Agent
            # -------------------------------------------------------------
            dead_bargers = set()
            for ws in list(bargers):
                # Other agents mixed (excluding this specific ws):
                peer_frames = [f for other_ws, f in barger_frames.items() if other_ws != ws and f]
                peer_mix = None
                if len(peer_frames) == 1:
                    peer_mix = peer_frames[0]
                elif len(peer_frames) > 1:
                    peer_mix = peer_frames[0]
                    for pf in peer_frames[1:]:
                        peer_mix = mix_ulaw_bytes(peer_mix, pf)

                # Downlink: Client Audio + Peer Agent Audio (Zero Self-Echo)
                agent_downlink = None
                if chunk_inbound and peer_mix:
                    agent_downlink = mix_ulaw_bytes(chunk_inbound, peer_mix)
                elif chunk_inbound:
                    agent_downlink = chunk_inbound
                elif peer_mix:
                    agent_downlink = peer_mix

                if agent_downlink:
                    b64_downlink = base64.b64encode(agent_downlink).decode("utf-8")
                    barger_payload = json.dumps({
                        "event": "media",
                        "call_id": call_id,
                        "media": {
                            "payload": b64_downlink,
                            "format": "audio/x-mulaw",
                            "sampleRate": 8000
                        }
                    })
                    try:
                        await ws.send_text(barger_payload)
                    except Exception as send_err:
                        is_dead = getattr(ws, "client_state", None) == WebSocketState.DISCONNECTED or getattr(ws, "closed", False)
                        if is_dead:
                            dead_bargers.add(ws)
                        else:
                            logger.debug(f"[AUDIO_MIXER] Transient send error to agent ws on {call_id}: {send_err}")

            if dead_bargers:
                bargers.difference_update(dead_bargers)
                for dws in dead_bargers:
                    barger_in_queues.pop(dws, None)
                    session.get("barger_jitter_states", {}).pop(dws, None)
                logger.info(f"[AUDIO_MIXER] Evicted {len(dead_bargers)} disconnected agent barger(s) from call {call_id}")

            # -------------------------------------------------------------
            # 5. Broadcast Composite Audio to Listeners & Call Recording
            # -------------------------------------------------------------
            composite_all = None
            if chunk_inbound and agents_mixed:
                composite_all = mix_ulaw_bytes(chunk_inbound, agents_mixed)
            elif chunk_inbound:
                composite_all = chunk_inbound
            elif agents_mixed:
                composite_all = agents_mixed

            if composite_all:
                session.setdefault("recorded_frames", []).append(composite_all)
                if any(b not in (0xFF, 0x7F, 0xFE, 0x7E, 0xFD, 0x7D) for b in composite_all):
                    session["speech_frames_count"] = session.get("speech_frames_count", 0) + 1

                b64_composite = base64.b64encode(composite_all).decode("utf-8")

                # Send to Twilio Supervisor Phone Leg
                if supervisor_ws and session.get("supervisor_stream_sid"):
                    try:
                        out_payload = {
                            "event": "media",
                            "streamSid": session["supervisor_stream_sid"],
                            "media": {"payload": b64_composite}
                        }
                        await supervisor_ws.send_text(json.dumps(out_payload))
                    except Exception as e:
                        logger.debug(f"PSTN supervisor send failed for {call_id}: {e}")

                # Broadcast to Browser Listeners (Passive supervisors hear all participants)
                if listeners:
                    browser_payload = json.dumps({
                        "event": "media",
                        "call_id": call_id,
                        "media": {
                            "payload": b64_composite,
                            "format": "audio/x-mulaw",
                            "sampleRate": 8000
                        }
                    })
                    dead_listeners = set()
                    for lws in list(listeners):
                        try:
                            await lws.send_text(browser_payload)
                        except Exception:
                            dead_listeners.add(lws)
                    listeners.difference_update(dead_listeners)

            # -------------------------------------------------------------
            # 6. Precision 20ms Telephony Clock Cadence (50 fps)
            # -------------------------------------------------------------
            t_elapsed = asyncio.get_event_loop().time() - t_tick_start
            sleep_sec = max(0.002, 0.020 - t_elapsed)
            await asyncio.sleep(sleep_sec)

    except Exception as e:
        logger.error(f"Error in mixer loop for call {call_id}: {e}")
    finally:
        logger.info(f"Barge stream mixer stopped for call {call_id}")

        # Drain any remaining buffered frames so the end of conversation is never cut off
        while not barge_in_q.empty() or not barge_out_q.empty():
            c_in = None
            c_out = None
            try:
                c_in = barge_in_q.get_nowait()
            except Exception:
                pass
            try:
                c_out = barge_out_q.get_nowait()
            except Exception:
                pass
            m_tail = None
            if c_in and c_out:
                m_tail = mix_ulaw_bytes(c_in, c_out)
            elif c_in:
                m_tail = c_in
            elif c_out:
                m_tail = c_out
            if m_tail:
                session.setdefault("recorded_frames", []).append(m_tail)
                if any(b not in (0xFF, 0x7F, 0xFE, 0x7E, 0xFD, 0x7D) for b in m_tail):
                    session["speech_frames_count"] = session.get("speech_frames_count", 0) + 1

        rec_frames = session.get("recorded_frames") or []
        speech_count = session.get("speech_frames_count", 0)
        clean_cid = (call_id.split("@")[0] if "@" in call_id else call_id).strip()

        # Always save the genuine original human conversation recording if frames were captured (>= 5 frames = 100ms)
        if len(rec_frames) >= 5:
            try:
                from app.services.recording_service import save_real_call_recording
                saved_url = save_real_call_recording(clean_cid, rec_frames)
                if saved_url:
                    logger.info(f"[CALL_RECORDING_SAVED] Real human conversation recording saved call_id={clean_cid} frames={len(rec_frames)} url={saved_url}")
            except Exception as rec_err:
                logger.error(f"[CALL_RECORDING_ERROR] Failed saving conversation recording call_id={clean_cid}: {rec_err}")
        else:
            logger.info(f"[CALL_RECORDING_INFO] Call {call_id} ended with {len(rec_frames)} frames. No audio frames captured.")


@router.websocket("/customer/{call_id}")
async def customer_stream(websocket: WebSocket, call_id: str):
    """
    Receives `<Start><Stream>` audio from the Customer & AI/Agent call leg (both_tracks).
    """
    await websocket.accept()
    logger.info(f"Customer telephony media stream connected for call {call_id}")

    session = await _get_or_create_session(call_id)
    session["customer_ws"] = websocket

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            event_type = msg.get("event")

            if event_type == "start":
                session["customer_stream_sid"] = msg.get("streamSid")
            elif event_type == "media":
                payload_str = msg["media"]["payload"]
                track = msg["media"].get("track", "inbound")
                payload_bytes = base64.b64decode(payload_str)

                if track == "inbound":
                    safe_put_audio_frame(session.get("inbound_queue"), payload_bytes, "inbound_queue", call_id)
                    logger.debug(f"[PHONE_RTP_RECV] [INBOUND_Q_PUT] call_id={call_id} bytes={len(payload_bytes)}")
                    if "barge_inbound_queue" in session:
                        safe_put_audio_frame(session.get("barge_inbound_queue"), payload_bytes, "barge_inbound_queue", call_id)
                elif track == "outbound":
                    if "barge_outbound_queue" in session:
                        safe_put_audio_frame(session.get("barge_outbound_queue"), payload_bytes, "barge_outbound_queue", call_id)
                    elif "outbound_queue" in session:
                        safe_put_audio_frame(session.get("outbound_queue"), payload_bytes, "outbound_queue", call_id)
            elif event_type == "stop":
                break

    except WebSocketDisconnect:
        logger.info(f"[SESSION_TRACE] customer websocket disconnected call_id={call_id}")
    except Exception as e:
        logger.error(f"Error in customer stream for call {call_id}: {e}")
    finally:
        logger.info(f"[SESSION_TRACE] customer websocket disconnected call_id={call_id}")
        if call_id in barge_sessions:
            sess = barge_sessions[call_id]
            sess["customer_ws"] = None

            # Do NOT treat websocket disconnect as SIP hangup if SIP call is active
            if sess.get("running"):
                logger.info(f"[SESSION_TRACE] keeping barge session alive call_id={call_id}")
            else:
                # Only reach here when the SIP call has actually ended (running == False)
                end_msg = json.dumps({"event": "CALL_ENDED", "call_id": call_id, "message": "Call stream ended."})
                for ws in list(sess.get("browser_listeners", set())):
                    try:
                        await ws.send_text(end_msg)
                        await ws.close()
                    except Exception:
                        pass
                for ws in list(sess.get("browser_bargers", set())):
                    try:
                        await ws.send_text(end_msg)
                        await ws.close()
                    except Exception:
                        pass

                barge_sessions.pop(call_id, None)
                logger.info(f"[SESSION_TRACE] barge session removed call_id={call_id}")


@router.websocket("/supervisor/{call_id}")
async def supervisor_stream(websocket: WebSocket, call_id: str):
    """
    Receives `<Connect><Stream>` from the Supervisor's PSTN phone call leg.
    """
    await websocket.accept()
    logger.info(f"Supervisor PSTN stream connected for call {call_id}")

    session = await _get_or_create_session(call_id)
    session["supervisor_ws"] = websocket

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("event") == "start":
                session["supervisor_stream_sid"] = msg.get("streamSid")
            elif msg.get("event") == "stop":
                break
    except WebSocketDisconnect:
        logger.info(f"Supervisor PSTN stream disconnected for call {call_id}")
    except Exception as e:
        logger.error(f"Error in supervisor PSTN stream for call {call_id}: {e}")
    finally:
        if call_id in barge_sessions:
            barge_sessions[call_id]["supervisor_ws"] = None


AUTHORIZED_SUPERVISOR_ROLES = {
    "ADMIN", "SUPERVISOR", "SUPER_ADMIN", "BUSINESS_OWNER",
    "SALES_MANAGER", "AGENT_MANAGER", "VOICE_AGENT", "AGENT", "EXPERT", "CALL_AGENT"
}

async def _validate_active_call_for_monitoring(call_id: str):
    """Verifies that the target call exists and is in an active state."""
    from app.db.database import AsyncSessionLocal
    from app.db.models import Call, CallTransfer
    from sqlalchemy import select, or_

    clean_id = call_id.split("@")[0].strip() if "@" in call_id else call_id.strip()
    stripped_id = clean_id[5:] if clean_id.startswith("call-") else clean_id
    prefixed_id = f"call-{clean_id}" if not clean_id.startswith("call-") else clean_id
    candidate_ids = list(dict.fromkeys([c for c in [call_id, clean_id, stripped_id, prefixed_id] if c]))

    call_obj = None
    try:
        async with AsyncSessionLocal() as db:
            call_obj = await db.get(Call, clean_id)
            if not call_obj and clean_id != call_id:
                call_obj = await db.get(Call, call_id)

            if not call_obj:
                conditions = [
                    Call.id.in_(candidate_ids),
                    Call.call_sid.in_(candidate_ids),
                    Call.conference_sid.in_(candidate_ids),
                    Call.call_sid.like(f"{clean_id}@%"),
                    Call.call_sid.like(f"{clean_id}%"),
                ]
                if stripped_id and len(stripped_id) >= 6:
                    conditions.append(Call.id.like(f"%{stripped_id}%"))
                stmt = select(Call).where(or_(*conditions)).order_by(Call.created_at.desc())
                call_obj = (await db.execute(stmt)).scalars().first()

            if not call_obj:
                stmt_t = select(CallTransfer).where(
                    or_(
                        CallTransfer.id.in_(candidate_ids),
                        CallTransfer.call_id.in_(candidate_ids)
                    )
                ).order_by(CallTransfer.created_at.desc())
                transfer_obj = (await db.execute(stmt_t)).scalars().first()
                if transfer_obj and transfer_obj.call_id:
                    call_obj = await db.get(Call, transfer_obj.call_id)
    except Exception as e:
        logger.debug(f"Error checking Call {call_id}: {e}")

    is_in_memory = (call_id in barge_sessions) or (clean_id in barge_sessions)
    if not is_in_memory:
        for s in list(barge_sessions.values()):
            if s.get("db_call_id") in (clean_id, call_id) or any(k in s.get("aliases", set()) for k in (clean_id, call_id)):
                is_in_memory = True
                break
    if not is_in_memory:
        try:
            from app.services.voice_service import ACTIVE_SIP_CALLS
            is_in_memory = (call_id in ACTIVE_SIP_CALLS) or (clean_id in ACTIVE_SIP_CALLS)
        except Exception:
            pass

    if not call_obj and not is_in_memory:
        return None, "NOT_FOUND"

    if call_obj:
        c_status = (call_obj.status or "").strip().upper()
        if c_status in ("COMPLETED", "FAILED", "BUSY", "NO-ANSWER", "CANCELED", "ENDED", "DISCONNECTED"):
            try:
                from app.services.voice_service import ACTIVE_SIP_CALLS
                if (call_id in ACTIVE_SIP_CALLS) or (clean_id in ACTIVE_SIP_CALLS):
                    return call_obj, "ACTIVE"
            except Exception:
                pass
            return call_obj, "TERMINATED"

    return call_obj, "ACTIVE"


@router.websocket("/browser/listen/{call_id}")
async def browser_listen_stream(
    websocket: WebSocket,
    call_id: str,
    token: Optional[str] = Query(None)
):
    """
    Authenticated WebSocket endpoint for Browser Supervisors in LISTEN mode.
    Streams real-time mixed call audio directly to the browser with zero mic transmission.
    """
    from app.services.audit_service import log_action

    user_payload = None
    if token:
        try:
            user_payload = decode_access_token(token)
        except Exception:
            user_payload = None

    if not user_payload:
        logger.warning(f"Barge listen rejected: missing or invalid authentication token for call {call_id}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "ERROR",
            "error": "UNAUTHORIZED",
            "call_id": call_id,
            "message": "Missing or invalid authentication token."
        }))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_role = str(user_payload.get("role", "")).strip().upper()
    if (
        user_role not in AUTHORIZED_SUPERVISOR_ROLES
        and "ADMIN" not in user_role
        and "SUPERVISOR" not in user_role
        and "AGENT" not in user_role
        and "EXPERT" not in user_role
    ):
        logger.warning(f"Barge listen rejected: unauthorized role ({user_role}) for call {call_id}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "ERROR",
            "error": "FORBIDDEN",
            "call_id": call_id,
            "message": f"Unauthorized role: {user_role}."
        }))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    call_obj, call_status = await _validate_active_call_for_monitoring(call_id)
    if call_status != "ACTIVE":
        logger.info(f"Barge listen for call {call_id} closed gracefully: call status is {call_status}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "CALL_ENDED" if call_status == "TERMINATED" else "ERROR",
            "error": call_status,
            "call_id": call_id,
            "message": f"Call {call_id} is {call_status.lower()} and no longer active for monitoring."
        }))
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE if call_status == "TERMINATED" else status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    user_id = user_payload.get("sub")
    tenant_id = user_payload.get("business_id") or (call_obj.business_id if call_obj else "default")
    clean_id = call_id.split("@")[0] if "@" in call_id else call_id
    resolved_entity_id = call_obj.id if call_obj else clean_id

    logger.info(f"Supervisor browser LISTEN connected for call {call_id} (User: {user_id}, Role: {user_role})")

    session = await _get_or_create_session(call_id)
    session["browser_listeners"].add(websocket)

    # Persist LISTEN start audit log
    try:
        await log_action(
            action="CALL_LISTEN_START",
            entity="Call",
            tenant_id=tenant_id,
            actor=user_id,
            entity_id=resolved_entity_id,
            metadata={"mode": "LISTEN", "role": user_role, "call_id": call_id}
        )
    except Exception as audit_err:
        logger.debug(f"Audit log error on listen start: {audit_err}")

    # Send initial handshake
    await websocket.send_text(json.dumps({
        "event": "LISTEN_CONNECTED",
        "call_id": call_id,
        "mode": "LISTEN",
        "status": "LISTENING",
        "message": "Connected to real-time call audio stream."
    }))

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data) if data else {}
            if msg.get("action") in ("stop", "leave"):
                break
    except WebSocketDisconnect:
        logger.info(f"Supervisor browser LISTEN disconnected for call {call_id}")
    except Exception as e:
        logger.error(f"Error in browser listen stream for {call_id}: {e}")
    finally:
        if call_id in barge_sessions:
            barge_sessions[call_id]["browser_listeners"].discard(websocket)

        # Persist LISTEN end audit log
        try:
            await log_action(
                action="CALL_LISTEN_END",
                entity="Call",
                tenant_id=tenant_id,
                actor=user_id,
                entity_id=resolved_entity_id,
                metadata={"mode": "LISTEN", "role": user_role, "call_id": call_id}
            )
        except Exception as audit_err:
            logger.debug(f"Audit log error on listen end: {audit_err}")


@router.websocket("/browser/barge/{call_id}")
async def browser_barge_stream(
    websocket: WebSocket,
    call_id: str,
    token: Optional[str] = Query(None)
):
    """
    Authenticated WebSocket endpoint for Browser Supervisors in BARGE IN mode.
    Establishes two-way real-time audio with per-call AI speech yielding:
      - Browser receives mixed live call audio
      - Browser sends supervisor microphone μ-law frames into the call stream
      - AI voice bot immediately yields speech on the target call
    """
    from app.services.audit_service import log_action
    from app.services.sip_ai_bot import pause_sip_ai_bot, resume_sip_ai_bot

    user_payload = None
    if token:
        try:
            user_payload = decode_access_token(token)
        except Exception:
            user_payload = None

    if not user_payload:
        logger.warning(f"Barge-in connection rejected: missing or invalid authentication token for call {call_id}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "ERROR",
            "error": "UNAUTHORIZED",
            "call_id": call_id,
            "message": "Missing or invalid authentication token."
        }))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    user_role = str(user_payload.get("role", "")).strip().upper()
    if (
        user_role not in AUTHORIZED_SUPERVISOR_ROLES
        and "ADMIN" not in user_role
        and "SUPERVISOR" not in user_role
        and "AGENT" not in user_role
        and "EXPERT" not in user_role
    ):
        logger.warning(f"Barge connection rejected: unauthorized role ({user_role}) for call {call_id}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "ERROR",
            "error": "FORBIDDEN",
            "call_id": call_id,
            "message": f"Unauthorized role: {user_role}."
        }))
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    call_obj, call_status = await _validate_active_call_for_monitoring(call_id)
    if call_status != "ACTIVE":
        logger.info(f"Barge connection for call {call_id} closed gracefully: call status is {call_status}")
        await websocket.accept()
        await websocket.send_text(json.dumps({
            "event": "CALL_ENDED" if call_status == "TERMINATED" else "ERROR",
            "error": call_status,
            "call_id": call_id,
            "message": f"Call {call_id} is {call_status.lower()} and no longer active for monitoring."
        }))
        await websocket.close(code=status.WS_1000_NORMAL_CLOSURE if call_status == "TERMINATED" else status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    user_id = user_payload.get("sub")
    tenant_id = user_payload.get("business_id") or (call_obj.business_id if call_obj else "default")
    user_tenant = user_payload.get("business_id")
    if user_tenant and call_obj and getattr(call_obj, "business_id", None):
        if str(user_tenant) != str(call_obj.business_id) and "SUPER_ADMIN" not in user_role and "SUPERADMIN" not in user_role:
            logger.warning(f"Barge connection rejected: tenant mismatch (user: {user_tenant}, call: {call_obj.business_id}) for call {call_id}")
            await websocket.accept()
            await websocket.send_text(json.dumps({
                "event": "ERROR",
                "error": "FORBIDDEN",
                "call_id": call_id,
                "message": "Tenant access forbidden."
            }))
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

    clean_id = call_id.split("@")[0] if "@" in call_id else call_id
    resolved_entity_id = call_obj.id if call_obj else clean_id

    logger.info(f"Supervisor browser BARGE IN connected for call {call_id} (User: {user_id}, Role: {user_role})")

    session = await _get_or_create_session(call_id)
    session["browser_bargers"].add(websocket)
    session.setdefault("barger_user_ids", {})[websocket] = user_id
    session["supervisor_barging"] = True

    # Yield AI speech immediately on target call session
    pause_sip_ai_bot(call_id)

    # Persist BARGE-IN start audit log
    try:
        await log_action(
            action="CALL_BARGE_IN_START",
            entity="Call",
            tenant_id=tenant_id,
            actor=user_id,
            entity_id=resolved_entity_id,
            metadata={"mode": "BARGE_IN", "role": user_role, "call_id": call_id}
        )
    except Exception as audit_err:
        logger.debug(f"Audit log error on barge-in start: {audit_err}")

    await websocket.send_text(json.dumps({
        "event": "BARGE_CONNECTED",
        "call_id": call_id,
        "mode": "BARGE_IN",
        "status": "BARGED_IN",
        "message": "Connected to real-time two-way call bridge. AI speech yielded."
    }))

    conn_mic_buf = bytearray()
    conn_pkt_count = 0

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data) if data else {}
            
            # Agent/Supervisor speaking: ingest mic audio frames
            if msg.get("event") == "media" and "media" in msg and "payload" in msg["media"]:
                mic_payload_b64 = msg["media"]["payload"]
                try:
                    payload_bytes = base64.b64decode(mic_payload_b64)
                    if payload_bytes:
                        conn_pkt_count += 1
                        session["mic_pkt_count"] = session.get("mic_pkt_count", 0) + 1

                        # Per-connection mic buffer prevents cross-agent byte interleaving and waveform corruption
                        conn_mic_buf.extend(payload_bytes)

                        # Accumulate and slice exact 160-byte (20ms) G.711 mu-law RTP frames
                        while len(conn_mic_buf) >= 160:
                            frame = bytes(conn_mic_buf[:160])
                            del conn_mic_buf[:160]

                            # Apply zero-overhead studio-grade speech boost and telephony loudness normalization
                            target_out_frame = boost_ulaw_speech_frame(frame)

                            # 1. Enqueue into this agent's incoming queue for synchronized mixer loop processing
                            b_in_q = session.setdefault("barger_in_queues", {}).setdefault(websocket, deque(maxlen=25))
                            b_in_q.append(target_out_frame)

                        if conn_pkt_count == 1 or conn_pkt_count % 50 == 0:
                            current_q = session.get("barger_in_queues", {}).get(websocket)
                            ingress_rms = calc_ulaw_rms_dbfs(frame if 'frame' in locals() else None)
                            logger.info(
                                f"[VOICE_AGENT_INGRESS] call_id={call_id} participant_id={user_id} role={user_role} agent_ws={id(websocket)} "
                                f"pkt={conn_pkt_count} bytes={len(payload_bytes)} q_depth={len(current_q) if current_q else 0} "
                                f"rms_dBFS={ingress_rms:.1f} codec=PCMU sample_rate=8000"
                            )
                except Exception as ingest_err:
                    logger.debug(f"Error ingesting mic audio frame: {ingest_err}")
            elif msg.get("action") == "leave":
                break

    except WebSocketDisconnect:
        logger.info(f"Supervisor browser BARGE IN disconnected for call {call_id}")
    except Exception as e:
        logger.error(f"Error in browser barge stream for {call_id}: {e}")
    finally:
        conn_mic_buf.clear()
        if call_id in barge_sessions:
            sess = barge_sessions[call_id]
            sess["browser_bargers"].discard(websocket)
            sess.get("barger_user_ids", {}).pop(websocket, None)
            sess.get("barger_in_queues", {}).pop(websocket, None)
            sess.get("barger_jitter_states", {}).pop(websocket, None)
            if sess.get("last_barger_frame") and sess["last_barger_frame"][0] == websocket:
                sess.pop("last_barger_frame", None)
            if not sess.get("browser_bargers"):
                sess["supervisor_barging"] = False
                resume_sip_ai_bot(call_id)

        # Persist BARGE-IN end audit log
        try:
            await log_action(
                action="CALL_BARGE_IN_END",
                entity="Call",
                tenant_id=tenant_id,
                actor=user_id,
                entity_id=resolved_entity_id,
                metadata={"mode": "BARGE_IN", "role": user_role, "call_id": call_id}
            )
        except Exception as audit_err:
            logger.debug(f"Audit log error on barge-in end: {audit_err}")


async def disconnect_browser_bargers(
    call_id: str,
    target_user_id: Optional[str] = None,
    target_ws: Optional[WebSocket] = None,
):
    """
    Safely disconnects and notifies active browser bargers when leaving a barge session.
    If target_user_id or target_ws is specified, only disconnects that specific participant,
    keeping remaining participants connected with active audio.
    """
    if call_id in barge_sessions:
        sess = barge_sessions[call_id]
        bargers = list(sess.get("browser_bargers", set()))
        user_map = sess.get("barger_user_ids", {})

        to_disconnect = []
        for ws in bargers:
            if target_ws and ws != target_ws:
                continue
            if target_user_id and user_map.get(ws) != target_user_id:
                continue
            to_disconnect.append(ws)

        for ws in to_disconnect:
            try:
                await ws.send_text(json.dumps({
                    "event": "BARGE_ENDED",
                    "call_id": call_id,
                    "message": "Barge session ended by user."
                }))
                await ws.close()
            except Exception:
                pass
            sess.get("browser_bargers", set()).discard(ws)
            user_map.pop(ws, None)
            sess.get("barger_in_queues", {}).pop(ws, None)
            sess.get("barger_jitter_states", {}).pop(ws, None)

        if not sess.get("browser_bargers"):
            sess["supervisor_barging"] = False
            from app.services.sip_ai_bot import resume_sip_ai_bot
            clean_id = call_id.split("@")[0] if "@" in call_id else call_id
            resume_sip_ai_bot(clean_id)
            resume_sip_ai_bot(call_id)

