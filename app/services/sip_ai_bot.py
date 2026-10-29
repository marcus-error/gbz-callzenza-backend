import asyncio
import io
import math
import re
import struct
import time
import wave
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import numpy as np
from loguru import logger
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import AsyncSessionLocal
from app.db.models import Call, Lead, AIAgent, Campaign, Conversation, Message
from app.services.lead_engagement_service import process_incoming_message
from app.services.sarvam_service import synthesize_speech_sarvam, transcribe_speech_sarvam
from app.services.tts_service import synthesize_for_call, synthesize_script_audio, get_effective_snapserve_key

settings = get_settings()

# Active bot tasks by call_id
ACTIVE_SIP_BOT_TASKS: Dict[str, asyncio.Task] = {}


def ulaw2pcm(u: int) -> int:
    """Decodes a single 8-bit G.711 mu-law byte to 16-bit linear PCM sample."""
    u = ~u & 0xFF
    t = ((u & 0x0F) << 3) + 0x84
    t <<= (u & 0x70) >> 4
    return -(t - 0x84) if (u & 0x80) else (t - 0x84)


def pcm2ulaw(pcm: int) -> int:
    """Encodes a 16-bit linear PCM sample (-32768 to 32767) into 8-bit G.711 mu-law."""
    BIAS = 132
    sign = 0x80 if pcm < 0 else 0
    if pcm < 0:
        pcm = -pcm
    if pcm > 32767:
        pcm = 32767
    pcm += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and (pcm & mask) == 0:
        exponent -= 1
        mask >>= 1
    mantissa = (pcm >> (exponent + 3)) & 0x0F
    return (~(sign | (exponent << 4) | mantissa)) & 0xFF


# Precomputed 16-bit linear PCM to 8-bit G.711 mu-law lookup table for instant vectorization
_PCM_TO_ULAW_TABLE = np.array([pcm2ulaw(s - 32768) for s in range(65536)], dtype=np.uint8)


def resample_and_normalize_audio(pcm_samples: np.ndarray, src_rate: int, target_rate: int = 8000) -> np.ndarray:
    """
    Studio-grade band-limited anti-aliasing resampling and telephony loudness normalization.
    - Zero resampling distortion when src_rate == target_rate (native 8kHz Murf audio passes bit-perfect).
    - 65-tap Blackman-windowed sinc filter (>60dB stopband attenuation, cutoff 3800Hz) when downsampling.
    - Balanced telephony headroom (target peak ~21,000 = -3.8 dBFS) preventing G.711 mu-law compression distortion,
      with smooth soft limiting so speech is loud, clear, and natural without clipping or hiss pumping.
    """
    if len(pcm_samples) == 0:
        return np.array([], dtype=np.int16)

    samples_float = pcm_samples.astype(np.float32)

    # 1. High-Fidelity Resampling
    if src_rate == target_rate or src_rate <= 0:
        resampled = samples_float
    elif src_rate > target_rate:
        # Steep anti-aliasing filter to eliminate high-frequency artifacts before decimation
        cutoff = 3800.0 / src_rate
        num_taps = 65
        n = np.arange(num_taps) - (num_taps - 1) / 2
        # Blackman window
        w = 0.42 - 0.5 * np.cos(2 * np.pi * np.arange(num_taps) / (num_taps - 1)) + 0.08 * np.cos(4 * np.pi * np.arange(num_taps) / (num_taps - 1))
        h = np.sinc(2 * cutoff * n) * w
        h /= np.sum(h)
        filtered = np.convolve(samples_float, h, mode='same')

        target_len = max(1, int(round(len(pcm_samples) * float(target_rate) / src_rate)))
        resampled = np.interp(np.linspace(0, len(pcm_samples) - 1, target_len), np.arange(len(pcm_samples)), filtered)
    else:
        target_len = max(1, int(round(len(pcm_samples) * float(target_rate) / src_rate)))
        resampled = np.interp(np.linspace(0, len(pcm_samples) - 1, target_len), np.arange(len(pcm_samples)), samples_float)

    # 2. Balanced Telephony Loudness & Anti-Clipping Soft Limiter
    # G.711 mu-law compresses heavily near full scale. Peak 21,000 gives optimal loudness with 0% distortion.
    peak = np.max(np.abs(resampled))
    if peak > 23000:
        # Tame loud bursts gently to prevent mu-law saturation
        resampled = resampled * (21000.0 / peak)
    elif peak > 200 and peak < 9000:
        # Gentle boost for very low-volume input without blowing up background noise
        gain = min(1.8, 16000.0 / peak)
        resampled = resampled * gain

    return np.clip(resampled, -32000, 32000).astype(np.int16)


def build_wav_from_pcm(pcm_samples: list[int], sample_rate: int = 8000) -> bytes:
    """Builds a valid 16-bit mono WAV in memory from PCM sample integers."""
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        raw_bytes = struct.pack(f"<{len(pcm_samples)}h", *pcm_samples)
        wf.writeframes(raw_bytes)
    return buf.getvalue()


def wav_to_mulaw(wav_bytes: bytes) -> bytes:
    """Converts WAV audio bytes into 8000Hz 8-bit G.711 mu-law bytes with anti-aliasing and loudness boost."""
    if not wav_bytes:
        return b""
    try:
        if not wav_bytes.startswith(b"RIFF"):
            try:
                import miniaudio
                decoded = miniaudio.decode(wav_bytes)
                raw_samples = np.frombuffer(decoded.samples, dtype=np.int16)
                if decoded.nchannels > 1:
                    raw_samples = raw_samples.reshape(-1, decoded.nchannels).mean(axis=1).astype(np.int16)
                processed = resample_and_normalize_audio(raw_samples, src_rate=decoded.sample_rate, target_rate=8000)
                indices = (processed.astype(np.int32) + 32768).clip(0, 65535)
                return _PCM_TO_ULAW_TABLE[indices].tobytes()
            except Exception as dec_err:
                logger.warning(f"[SIP AI BOT] Non-RIFF audio decode fallback notice: {dec_err}")

        with io.BytesIO(wav_bytes) as bio:
            with wave.open(bio, 'rb') as wf:
                n_channels = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                framerate = wf.getframerate()
                frames = wf.readframes(wf.getnframes())

        if sampwidth == 2:
            raw_samples = np.frombuffer(frames, dtype=np.int16)
            if n_channels > 1:
                raw_samples = raw_samples.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

            processed = resample_and_normalize_audio(raw_samples, src_rate=framerate, target_rate=8000)
            indices = (processed.astype(np.int32) + 32768).clip(0, 65535)
            return _PCM_TO_ULAW_TABLE[indices].tobytes()
        elif sampwidth == 1:
            raw_samples = (np.frombuffer(frames, dtype=np.uint8).astype(np.int16) - 128) * 256
            processed = resample_and_normalize_audio(raw_samples, src_rate=framerate, target_rate=8000)
            indices = (processed.astype(np.int32) + 32768).clip(0, 65535)
            return _PCM_TO_ULAW_TABLE[indices].tobytes()
    except Exception as e:
        logger.warning(f"[SIP AI BOT] wav_to_mulaw conversion warning: {e}")
    return b""


async def synthesize_text_to_mulaw(text: str, agent: Optional[AIAgent] = None, is_local_test: bool = False, call_id: Optional[str] = None, turn_id: Optional[int] = None, **kwargs: Any) -> bytes:
    """Synthesizes text into 8000Hz G.711 mu-law audio bytes using SnapServe AI, Sarvam, or Neural Edge."""
    clean_text = re.sub(r'<[^>]+>', ' ', text).strip()
    if not clean_text:
        return b""

    logger.info(f"[TTS] call_id={call_id or 'unknown'} generation_started text=\"{clean_text[:50]}\"")
    t_start = time.perf_counter()
    is_ta = any('\u0B80' <= c <= '\u0BFF' for c in clean_text)
    lang_code = "ta-IN" if is_ta else "en-IN"

    def _log_tts_success(provider_name: str, mulaw_bytes: bytes):
        dur_ms = int((time.perf_counter() - t_start) * 1000)
        mulaw_len = len(mulaw_bytes)
        pcm_samples = [ulaw2pcm(b) for b in mulaw_bytes]
        audio_dur_sec = len(pcm_samples) / 8000.0 if pcm_samples else 0.0
        sum_sq = sum(s * s for s in pcm_samples)
        rms = math.sqrt(sum_sq / max(1, len(pcm_samples))) if pcm_samples else 0.0
        peak = max(abs(s) for s in pcm_samples) if pcm_samples else 0

        logger.info(f"[TTS] call_id={call_id or 'unknown'} text=\"{clean_text[:50]}\" provider={provider_name} audio_generated=true audio_format=WAV sample_rate=8000 channels=1 sample_width=2 duration_sec={audio_dur_sec:.2f} byte_count={mulaw_len} rms={rms:.1f} peak={peak} duration_ms={dur_ms}")
        logger.info(
            f"[TTS_TX]\n"
            f"sample_rate=8000\n"
            f"channels=1\n"
            f"sample_width=2\n"
            f"duration={audio_dur_sec:.2f}\n"
            f"bytes={mulaw_len * 2}"
        )
        logger.info(f"[TTS] generation_completed duration={dur_ms}ms")
        logger.info("[TTS] audio_format=WAV")
        logger.info("[TTS] sample_rate=8000")
        logger.info("[TTS] channels=1")
        logger.info("[TTS] sample_width=2")
        logger.info("[TTS] codec=PCMU")
        logger.info(f"[SIP AI BOT] {provider_name} generated {mulaw_len} bytes of 8kHz mu-law audio (RMS={rms:.1f}, peak={peak})")

    # 0. Campaign-Level Dynamic Voice Configuration Override (Per-Campaign / Call Isolation)
    voice_config = kwargs.get("voice_config")
    campaign = kwargs.get("campaign")
    business_id = kwargs.get("business_id")
    db = kwargs.get("db")

    if not voice_config and campaign:
        voice_config = getattr(campaign, "voice_config", None)
        if not voice_config and isinstance(getattr(campaign, "schedule", None), dict):
            voice_config = campaign.schedule.get("voice_config")

    if not voice_config and agent:
        v_cfg_id = getattr(agent, "voice_config_id", None)
        if v_cfg_id:
            try:
                from app.db.models import Integration
                integ = None
                if db:
                    integ = await db.get(Integration, v_cfg_id)
                else:
                    from app.db.database import AsyncSessionLocal
                    async with AsyncSessionLocal() as session_db:
                        integ = await session_db.get(Integration, v_cfg_id)
                if integ and integ.is_active:
                    cfg_data = integ.config or {}
                    voice_config = {
                        "id": integ.id,
                        "provider": integ.provider,
                        "voice_id": cfg_data.get("voice_id"),
                        "voice_name": cfg_data.get("voice_name") or cfg_data.get("name"),
                        "model_id": cfg_data.get("model_id"),
                        "voice_link": cfg_data.get("voice_link") or cfg_data.get("endpoint_url"),
                        "endpoint_url": cfg_data.get("endpoint_url") or cfg_data.get("voice_link"),
                        "api_key": cfg_data.get("api_key"),
                        "settings": cfg_data.get("settings", {}),
                        "is_active": True,
                    }
            except Exception as e:
                logger.debug(f"[SIP AI BOT] Could not load agent voice_config {v_cfg_id}: {e}")

        if not voice_config and getattr(agent, "voice_provider", None):
            voice_config = {
                "provider": getattr(agent, "voice_provider", None),
                "voice_id": getattr(agent, "voice_id", None),
                "voice_name": getattr(agent, "voice_name", None),
                "model_id": getattr(agent, "voice_model_id", None),
                "is_active": True,
            }

    if voice_config and isinstance(voice_config, dict) and voice_config.get("is_active", True) is not False:
        try:
            from app.services.tts_service import synthesize_from_voice_config
            prov_name = str(voice_config.get("provider", "custom")).lower()
            effective_bid = business_id or (getattr(campaign, "business_id", None) if campaign else (getattr(agent, "business_id", None) if agent else None))
            wav_bytes = await synthesize_from_voice_config(
                clean_text,
                voice_config=voice_config,
                agent=agent,
                business_id=effective_bid,
                db=db,
                requested_format="wav",
                sample_rate=8000,
            )
            if wav_bytes:
                mulaw = wav_to_mulaw(wav_bytes)
                if mulaw:
                    v_label = voice_config.get("voice_name") or voice_config.get("voice_id") or voice_config.get("reference_id") or "default"
                    _log_tts_success(f"{prov_name.upper()} (Voice: {v_label})", mulaw)
                    return mulaw
        except Exception as e:
            logger.warning(f"[SIP AI BOT] voice_config synthesis failed: {e}")

    active_provider = (getattr(settings, "tts_provider", None) or "murf").lower().strip()

    # 1. Murf AI TTS (when explicitly configured as provider and Murf key available)
    if active_provider == "murf":
        try:
            from app.services.tts_service import synthesize_murf_speech, is_murf_available
            if is_murf_available():
                raw_v_id = getattr(agent, "voice_id", None) or settings.murf_voice_id or "Pooja"
                voice_id = "Pooja" if (not raw_v_id or raw_v_id.lower() in ("default", "auto")) else raw_v_id
                model_id = getattr(agent, "voice_model_id", None) or settings.murf_model or "Falcon"
                wav_bytes = await synthesize_murf_speech(
                    clean_text,
                    voice_id=voice_id,
                    model=model_id,
                    audio_format="WAV",
                    sample_rate=8000,
                )
                if wav_bytes:
                    mulaw = wav_to_mulaw(wav_bytes)
                    if mulaw:
                        _log_tts_success(f"Murf AI (voice={voice_id})", mulaw)
                        return mulaw
        except Exception as e:
            logger.warning(f"[SIP AI BOT] Murf AI TTS synthesis notice: {e}")

    # 2. Try SnapServe AI TTS (when active_provider == snapserve or fallback) — request native 8000Hz WAV for zero-distortion telephony
    if active_provider == "snapserve" or (active_provider != "murf" and get_effective_snapserve_key()):
        try:
            from app.services.tts_service import synthesize_snapserve_speech, resolve_snapserve_voice_id
            agent_vid = getattr(agent, "voice_id", None)
            voice_id = resolve_snapserve_voice_id(agent_vid)
            wav_bytes = await synthesize_snapserve_speech(
                clean_text,
                voice_id=voice_id,
                format="wav",
                sample_rate=8000,
            )
            if wav_bytes:
                mulaw = wav_to_mulaw(wav_bytes)
                if mulaw:
                    _log_tts_success(f"SnapServe AI (voice={voice_id})", mulaw)
                    return mulaw
        except Exception as e:
            logger.warning(f"[SIP AI BOT] SnapServe AI TTS synthesis notice: {e}")

    # 2. Try Sarvam TTS for Indic languages
    if getattr(settings, "sarvam_api_key", "") and is_ta:
        try:
            wav_bytes = await synthesize_speech_sarvam(
                text=clean_text,
                target_language_code=lang_code,
                speech_sample_rate=8000,
            )
            if wav_bytes:
                mulaw = wav_to_mulaw(wav_bytes)
                if mulaw:
                    _log_tts_success("Sarvam TTS", mulaw)
                    return mulaw
        except Exception as e:
            logger.warning(f"[SIP AI BOT] Sarvam TTS synthesis notice: {e}")

    # 4. Universal Fallback via gTTS + miniaudio for zero-credential guarantee
    try:
        import io
        import miniaudio
        from gtts import gTTS
        buf = io.BytesIO()
        gTTS(text=clean_text, lang='ta' if is_ta else 'en').write_to_fp(buf)
        mp3_bytes = buf.getvalue()
        if mp3_bytes:
            decoded = miniaudio.decode(mp3_bytes)
            raw_samples = np.frombuffer(decoded.samples, dtype=np.int16)
            if decoded.nchannels > 1:
                raw_samples = raw_samples.reshape(-1, decoded.nchannels).mean(axis=1).astype(np.int16)
            processed = resample_and_normalize_audio(raw_samples, src_rate=decoded.sample_rate, target_rate=8000)
            indices = (processed.astype(np.int32) + 32768).clip(0, 65535)
            mulaw = _PCM_TO_ULAW_TABLE[indices].tobytes()
            if mulaw:
                _log_tts_success("gTTS fallback", mulaw)
                return mulaw
    except Exception as e:
        logger.warning(f"[SIP AI BOT] gTTS fallback synthesis notice: {e}")

    return b""


async def broadcast_state_change(call_id: str, turn_id: int, new_state: str, reason: str = ""):
    """Broadcasts authoritative state transition event to WebSockets for live UI synchrony."""
    try:
        import json
        from app.api.routes.barge_stream import barge_sessions
        from app.api.routes.websockets import global_ws_manager
        sess = barge_sessions.get(call_id, {})
        has_rtp_out = bool(sess.get("rtp_outbound_queue") and len(sess["rtp_outbound_queue"]) > 0)
        has_in_q = bool(sess.get("inbound_queue"))
        logger.info(f"[STATE_TRACE] call_id={call_id} turn_id={turn_id} state={new_state} reason=\"{reason}\"")
        logger.info(
            f"[SESSION_DEBUG] session_id={call_id} call_id={call_id} "
            f"sip_state=CONNECTED call_status=IN_PROGRESS "
            f"websocket_state=BROADCASTING rtp_state={'TX_ACTIVE' if has_rtp_out else ('RX_ACTIVE' if has_in_q else 'IDLE')} "
            f"ai_state={new_state}"
        )
        await global_ws_manager.broadcast(json.dumps({
            "event": "STATE_CHANGE",
            "type": "STATE_CHANGE",
            "call_id": call_id,
            "turn_id": turn_id,
            "state": new_state,
            "reason": reason,
        }))
    except Exception as e:
        logger.warning(f"[STATE_CHANGE] Broadcast notice: {e}")


async def broadcast_transcript(
    call_id: str,
    sender: str,
    text: str,
    status: str = "CONNECTED",
    turn_id: Optional[int] = 1,
    event: Optional[str] = None,
    role: Optional[str] = None,
    is_partial: bool = False,
    is_final: bool = True,
    **kwargs: Any,
):
    """Broadcasts live transcript to WebSockets so Voice Agent and AI calling dashboards update."""
    try:
        import json
        from app.api.routes.websockets import global_ws_manager
        is_user = sender.lower() in ("customer", "user", "lead")
        evt_name = event or ("CUSTOMER_TRANSCRIPT_FINAL" if is_user else "TRANSCRIPT_UPDATE")
        resolved_role = role or ("user" if is_user else "assistant")
        payload = {
            "event": evt_name,
            "type": evt_name,
            "call_id": call_id,
            "turn_id": turn_id,
            "sender": sender,
            "role": resolved_role,
            "text": text,
            "message": f"{sender}: {text}",
            "status": status,
            "is_partial": is_partial,
            "is_final": is_final,
        }
        for k, v in kwargs.items():
            if v is not None:
                payload[k] = v
        await global_ws_manager.broadcast(json.dumps(payload))
    except Exception:
        pass


async def _run_sip_ai_bot(call_id: str, is_inbound: bool):
    """
    Continuous AI Voice Bot loop running on an active SIP call.
    - Speaks the RAG / Campaign opening greeting with clear, natural, loud audio.
    - Buffers incoming customer voice with 240ms pre-roll and echo suppression.
    - Supports real-time barge-in interruption.
    - Transcribes caller voice cleanly using Gemini STT.
    - Answers dynamically from RAG knowledge base.
    - Streams 8kHz G.711 mu-law audio over RTP.
    """
    logger.info(f"[SIP AI BOT] Starting AI Voice Bot for call {call_id} (inbound={is_inbound})")
    session_id = call_id
    logger.info(f"[AI_STARTUP] call_id={call_id} session_id={session_id} direction={'INBOUND' if is_inbound else 'OUTBOUND'} agent_type=AI_VOICE timestamp={datetime.now(timezone.utc).isoformat()}")
    from app.api.routes.barge_stream import barge_sessions

    # Wait up to 5s for barge session to be initialized
    session = None
    for _ in range(50):
        session = (
            barge_sessions.get(call_id)
            or (barge_sessions.get(call_id.split("@")[0]) if "@" in call_id else None)
        )
        if session:
            break
        await asyncio.sleep(0.1)

    if not session:
        # Check by database call record aliases
        try:
            async with AsyncSessionLocal() as db_check:
                c_check = await db_check.get(Call, call_id)
                if not c_check and "@" in call_id:
                    c_check = (await db_check.execute(select(Call).where(Call.call_sid == call_id))).scalars().first()
                if c_check:
                    for k in [c_check.id, c_check.call_sid, (c_check.call_sid.split("@")[0] if c_check.call_sid else None)]:
                        if k and k in barge_sessions:
                            session = barge_sessions[k]
                            break
        except Exception:
            pass

    if not session:
        logger.warning(f"[SIP AI BOT] No active barge session found for call {call_id}. Exiting.")
        return

    # 1. Resolve DB records: Call, Lead, AIAgent, Campaign
    lead = None
    agent = None
    campaign = None
    conversation = None

    async with AsyncSessionLocal() as db:
        call = None
        for _ in range(30):
            call = await db.get(Call, call_id)
            if call:
                break
            await asyncio.sleep(0.1)

        if not call:
            logger.warning(f"[SIP AI BOT] Call record {call_id} not found in DB after retries.")
            return

        # Authoritative check: Call record persisted agent_type is the primary authority.
        # AI bot must NEVER run on human Voice Agent calls.
        is_voice_agent = False
        decision_reason = "default_ai"
        camp_primary_type = "NONE"
        transfer_agent_ids = "NONE"
        camp_id_val = call.campaign_id or "NONE"

        if call.campaign_id:
            camp = await db.get(Campaign, call.campaign_id)
            if camp:
                camp_primary_type = (camp.agent_type or "AI_VOICE").upper()
                transfer_agent_ids = camp.voice_agent_id or "NONE"

        call_agent_type_upper = (call.agent_type or "").upper()
        if (
            call_agent_type_upper in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
            or "HUMAN" in call_agent_type_upper
        ):
            is_voice_agent = True
            decision_reason = "call_record_persisted_human"
        elif call_agent_type_upper in ("AI_VOICE", "AI", "AI_AGENT"):
            is_voice_agent = False
            decision_reason = "call_record_persisted_ai"
        elif session.get("is_voice_agent_call") is True or (session.get("agent_type") or "").upper() in ("HUMAN", "VOICE", "VOICE_AGENT"):
            is_voice_agent = True
            decision_reason = "session_metadata_human"
        elif call.campaign_id and camp:
            if (
                camp_primary_type in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT", "VOICE_AGENT_HUMAN")
                or "HUMAN" in camp_primary_type
            ):
                is_voice_agent = True
                decision_reason = "campaign_configured_as_human"
            else:
                is_voice_agent = False
                decision_reason = "campaign_configured_as_ai"
        elif call.lead_id:
            lead_check = await db.get(Lead, call.lead_id)
            if lead_check and lead_check.assigned_expert_id and not lead_check.agent_id:
                is_voice_agent = True
                decision_reason = "lead_assigned_to_expert"

        # Synchronize session flags with authoritative DB routing decision
        session["is_voice_agent_call"] = is_voice_agent
        session["agent_type"] = "HUMAN" if is_voice_agent else "AI_VOICE"

        logger.info(
            f"[CALL_ROUTING] call_id={call_id} direction={call.direction or 'OUTBOUND'} "
            f"campaign_id={camp_id_val} campaign_primary_agent_type={camp_primary_type} "
            f"primary_agent_id={call.agent_id} transfer_agent_ids={transfer_agent_ids} "
            f"resolved_agent_type={'HUMAN' if is_voice_agent else 'AI_VOICE'} "
            f"decision_reason='{decision_reason}' "
            f"ai_bot={'SKIPPED' if is_voice_agent else 'STARTED'}"
        )

        if is_voice_agent:
            session["ai_speaking"] = False
            ai_q = session.get("ai_rtp_tx_queue")
            if ai_q is not None and len(ai_q) > 0:
                ai_q.clear()
            logger.info(
                f"[SIP AI BOT] Call {call_id} is assigned to human Voice Agent "
                f"(agent_type={call.agent_type}, agent_id={call.agent_id}, user_id={call.user_id}). "
                f"Aborting AI bot: AI must NOT speak or reply on human Voice Agent calls."
            )
            return

        # Resolve AIAgent
        agent = None
        if call.agent_id:
            agent = await db.get(AIAgent, call.agent_id)
        if not agent:
            # Fallback to active AIAgent
            agent = (await db.execute(select(AIAgent).where(AIAgent.status == "ACTIVE"))).scalars().first()
            if not agent:
                agent = (await db.execute(select(AIAgent).order_by(AIAgent.created_at.asc()))).scalars().first()
            if agent:
                call.agent_id = agent.id
                call.agent_type = "AI_VOICE"
                db.add(call)
                await db.commit()
            else:
                logger.info(f"[SIP AI BOT] Call {call_id} has no assigned AI agent and none found in DB. Aborting AI bot.")
                return

        # Resolve Lead
        if call.lead_id:
            lead = await db.get(Lead, call.lead_id)
        elif call.from_number:
            lead = (await db.execute(select(Lead).where(Lead.phone == call.from_number))).scalars().first()
            if not lead and call.to_number:
                lead = (await db.execute(select(Lead).where(Lead.phone == call.to_number))).scalars().first()
            if not lead:
                lead = Lead(
                    business_id=call.business_id or (agent.business_id if agent else ""),
                    source="PHONE",
                    phone=call.from_number if is_inbound else call.to_number,
                    first_name="Customer",
                    status="ENGAGED",
                )
                db.add(lead)
                await db.flush()
            call.lead_id = lead.id

        # Resolve Campaign with script
        if call.campaign_id:
            campaign = await db.get(Campaign, call.campaign_id)
        if not campaign and call.lead_id:
            from app.db.models import CampaignLead
            cl_stmt = select(CampaignLead).where(CampaignLead.lead_id == call.lead_id).order_by(CampaignLead.created_at.desc())
            cl = (await db.execute(cl_stmt)).scalars().first()
            if cl and cl.campaign_id:
                campaign = await db.get(Campaign, cl.campaign_id)
        if campaign and not call.campaign_id:
            call.campaign_id = campaign.id

        # Resolve or create Conversation
        if call.conversation_id:
            conversation = await db.get(Conversation, call.conversation_id)
        if not conversation:
            conversation = Conversation(
                business_id=call.business_id or (agent.business_id if agent else ""),
                lead_id=lead.id if lead else None,
                agent_id=agent.id if agent else None,
                channel="VOICE",
                status="ACTIVE",
            )
            db.add(conversation)
            await db.flush()
            call.conversation_id = conversation.id

        call.status = "IN_PROGRESS"
        db.add(call)
        await db.commit()

    logger.info(f"[SIP AI BOT] Connected AI Agent {agent.id if agent else 'None'} to call {call_id} with Campaign {campaign.name if campaign else 'None'}")
    camp_voice_cfg = getattr(campaign, "voice_config", None) or (campaign.schedule.get("voice_config") if (campaign and isinstance(campaign.schedule, dict)) else None)
    biz_id = getattr(call, "business_id", None) or (getattr(campaign, "business_id", None) if campaign else None) or (getattr(agent, "business_id", None) if agent else None)

    # 2. Generate Opening Greeting from Session, Campaign Script, or RAG
    turn_id = 1
    opening_line = session.get("initial_greeting")

    if session.get("initial_greeting_sent"):
        logger.info(
            f"[GREETING_DEBUG] source=sip_ai_bot session_id={session_id} call_id={call_id} "
            f"campaign_id={campaign.id if campaign else None} status=SKIPPED_ALREADY_SENT "
            f"timestamp={datetime.now(timezone.utc).isoformat()}"
        )
    else:
        if not opening_line:
            from app.services.ai.campaign_greeting_service import CampaignGreetingService
            call_direction = (getattr(call, "direction", None) or "outbound").lower()
            opening_line = CampaignGreetingService.resolve_campaign_greeting(
                campaign=campaign,
                lead=lead,
                agent=agent,
                call_direction=call_direction,
                session_id=session_id,
                call_id=call_id,
            )

        if not opening_line and campaign:
            camp_script = getattr(campaign, "script", None) or (campaign.schedule.get("script") if isinstance(campaign.schedule, dict) else None)
            if camp_script:
                from app.api.routes.voice import extract_campaign_script_greeting
                opening_line = extract_campaign_script_greeting(camp_script, lead, agent)

        if not opening_line:
            raw_lead = (lead.first_name if (lead and lead.first_name and lead.first_name != "Customer") else "") or ""
            lead_name = "" if (raw_lead.lower().startswith("caller ") or re.search(r'\d{6,}', raw_lead)) else raw_lead
            agent_name = (agent.name if agent and agent.name else "Alex")
            biz_name = (campaign.name if campaign else "our team")
            if lead_name:
                opening_line = f"Hello {lead_name}, this is {agent_name} from {biz_name}. How can I help you today?"
            else:
                opening_line = f"Hello, this is {agent_name} from {biz_name}. How can I help you today?"

        from app.services.ai.response_sanitizer import ResponseSanitizer, ResponseGuard
        opening_line = ResponseSanitizer.clean_customer_facing_text(
            opening_line,
            agent_name=agent.name if agent else "Alex",
            lead_name=lead.first_name if lead else ""
        )
        guard_res = ResponseGuard.validate_customer_response(opening_line)
        if not guard_res["is_valid"]:
            logger.warning(f"[RESPONSE_GUARD] Opening greeting rejected: {guard_res['reason']}. Falling back to default greeting.")
            raw_lead = (lead.first_name if (lead and lead.first_name and lead.first_name != "Customer") else "") or ""
            lead_name = "" if (raw_lead.lower().startswith("caller ") or re.search(r'\d{6,}', raw_lead)) else raw_lead
            agent_name = (agent.name if agent and agent.name else "Alex")
            biz_name = (campaign.name if campaign else "our team")
            if lead_name:
                opening_line = f"Hello {lead_name}, this is {agent_name} from {biz_name}. How can I help you today?"
            else:
                opening_line = f"Hello, this is {agent_name} from {biz_name}. How can I help you today?"

    logger.info(
        f"[GREETING_DEBUG] source=sip_ai_bot session_id={session_id} call_id={call_id} "
        f"campaign_id={campaign.id if campaign else None} generated_text=\"{opening_line}\" "
        f"timestamp={datetime.now(timezone.utc).isoformat()}"
    )

    logger.info(f"[GREETING_TEXT_READY] call_id={call_id} session_id={session_id} char_count={len(opening_line)} text=\"{opening_line}\" timestamp={datetime.now(timezone.utc).isoformat()}")
    logger.info(f"[SIP AI BOT] Opening greeting: '{opening_line}'")

    if not session.get("initial_greeting_logged_in_db"):
        session["initial_greeting_logged_in_db"] = True
        session["initial_greeting_sent"] = True

        # Record message in conversation
        async with AsyncSessionLocal() as db:
            msg = Message(
                conversation_id=conversation.id,
                sender_type="AI",
                sender_id=agent.id if agent else "AI",
                content=opening_line,
            )
            db.add(msg)
            call_obj = await db.get(Call, call_id)
            if call_obj:
                call_obj.transcript = f"Agent: {opening_line}\n"
                call_obj.turn_count = 1
                db.add(call_obj)
            await db.commit()

        await broadcast_state_change(call_id, turn_id, "CONNECTED", "SIP call connected, initiating AI bot pipeline")
        await broadcast_transcript(call_id, "Agent", opening_line, turn_id=turn_id)

    # Helper function to fan out single generated TTS audio to both Phone RTP and Laptop Monitor queues
    def fanout_ai_audio(mulaw_bytes: bytes, t_id: int):
        if not mulaw_bytes or session.get("supervisor_barging"):
            return
        chunk_size = 160
        phone_bytes_cnt = 0
        for i in range(0, len(mulaw_bytes), chunk_size):
            slice_bytes = mulaw_bytes[i:i + chunk_size]
            if len(slice_bytes) < chunk_size:
                slice_bytes += b'\xff' * (chunk_size - len(slice_bytes))

            # 1. Fan out to dedicated AI Phone RTP queue (single authoritative queue for AI audio)
            # The RTP transmission loop in voice_service/sip_inbound_daemon meters frames to RTP
            # and feeds barge_outbound_queue at real-time 20ms cadence, ensuring audio is recorded
            # exactly once with perfect customer audio time alignment.
            if "ai_rtp_tx_queue" in session:
                session["ai_rtp_tx_queue"].append(slice_bytes)
                phone_bytes_cnt += len(slice_bytes)

        logger.info(
            f"[AI_AUDIO_FANOUT] call_id={call_id} turn_id={t_id} "
            f"pcm_bytes={len(mulaw_bytes)*2} phone_queue_bytes={phone_bytes_cnt}"
        )

        logger.info(f"[GREETING_RTP_QUEUE] call_id={call_id} session_id={session_id} enqueued_frames={math.ceil(len(mulaw_bytes)/160)} queue_len={len(session.get('ai_rtp_tx_queue', []))} timestamp={datetime.now(timezone.utc).isoformat()}")
    # 3. Stream Greeting Audio to Caller
    session["ai_speaking"] = True
    logger.info(f"[AI_AUDIO_START] call_id={call_id} turn_id={turn_id}")
    logger.info(f"[CALL_STATE] CALL_ANSWERED call_id={call_id}")
    await broadcast_state_change(call_id, turn_id, "AI_SPEAKING", "Synthesizing and streaming opening greeting")
    logger.info(f"[GREETING] trigger=CALL_ANSWERED")
    logger.info(f"[GREETING] started")
    logger.info(f"[CALL_STATE] call_id={call_id} previous=CONNECTED new=AI_SPEAKING reason=CALL_ANSWERED_GREETING timestamp={datetime.now(timezone.utc).isoformat()}")
    logger.info(f"[TTS] START call_id={call_id} turn_id={turn_id}")
    logger.info(f"[TTS_START] call_id={call_id} turn_id={turn_id}")
    logger.info(
        f"[TTS_DEBUG] request_id={turn_id} session_id={session_id} call_id={call_id} "
        f"text=\"{opening_line}\" reason=INITIAL_OUTBOUND_GREETING"
    )
    logger.info(f"[GREETING_TTS_START] call_id={call_id} session_id={session_id} text=\"{opening_line[:60]}\" timestamp={datetime.now(timezone.utc).isoformat()}")
    greeting_mulaw = await synthesize_text_to_mulaw(
        opening_line,
        agent=agent,
        call_id=call_id,
        campaign=campaign,
        voice_config=camp_voice_cfg,
        business_id=biz_id,
    )
    if greeting_mulaw:
        num_frames = math.ceil(len(greeting_mulaw) / 160)
        dur_sec = len(greeting_mulaw) / 8000.0
        logger.info(f"[GREETING_TTS_SUCCESS] call_id={call_id} session_id={session_id} byte_count={len(greeting_mulaw)} duration_sec={dur_sec:.2f} timestamp={datetime.now(timezone.utc).isoformat()}")
        logger.info(f"[GREETING_AUDIO_CONVERTED] call_id={call_id} session_id={session_id} codec=PCMU sample_rate=8000 channels=1 bytes={len(greeting_mulaw)} frames={num_frames} timestamp={datetime.now(timezone.utc).isoformat()}")
        logger.info(f"[TTS_PCM_READY] call_id={call_id} turn_id={turn_id} bytes={len(greeting_mulaw)}")
        logger.info(f"[RTP_TX_FIRST] call_id={call_id} turn_id={turn_id}")
        logger.info(f"[AI_AUDIO_TX] call_id={call_id} turn_id={turn_id} bytes={len(greeting_mulaw)}")
        fanout_ai_audio(greeting_mulaw, turn_id)
        logger.info(f"[RTP_TX_LAST] call_id={call_id} turn_id={turn_id}")
    else:
        logger.error(f"[GREETING_TTS_ERROR] call_id={call_id} session_id={session_id} reason=SYNTHESIS_RETURNED_EMPTY timestamp={datetime.now(timezone.utc).isoformat()}")
        logger.error(f"[VOICE_PIPELINE_ERROR] call_id={call_id} turn_id={turn_id} reason=TTS_GENERATION_FAILED")

    # Wait until dedicated outbound AI audio queue is completely drained before listening
    ai_tx_q = session.get("ai_rtp_tx_queue")
    while session.get("running") and ai_tx_q is not None and len(ai_tx_q) > 0:
        await asyncio.sleep(0.02)

    # Defensive check: if session was cancelled/hung up during queue drain, exit cleanly without advancing state
    if not session.get("running"):
        logger.info(f"[SIP AI BOT] Call session {call_id} ended during audio playback. Aborting state transition.")
        return

    # Allow physical UDP socket buffer playback to complete before switching state
    await asyncio.sleep(0.30)
    if not session.get("running"):
        return

    session["ai_speaking"] = False
    logger.info(f"[AI_AUDIO_END] call_id={call_id} turn_id={turn_id}")

    logger.info(f"[TTS] COMPLETE call_id={call_id} turn_id={turn_id}")
    logger.info(f"[RTP_PLAYBACK_COMPLETE] call_id={call_id} turn_id={turn_id}")
    logger.info(f"[AI_AUDIO_PLAYBACK_COMPLETE] call_id={call_id} turn_id={turn_id}")
    logger.info(f"[GREETING] completed")

    if not session.get("running"):
        return

    await broadcast_state_change(call_id, turn_id, "RETURNING_TO_LISTENING", "Greeting TTS completed")

    if not session.get("running"):
        return

    logger.info(f"[STATE] RETURNING_TO_LISTENING -> LISTENING_FOR_CUSTOMER")
    logger.info(f"[CALL_STATE] LISTENING_TO_CUSTOMER call_id={call_id} previous=RETURNING_TO_LISTENING new=LISTENING_FOR_CUSTOMER reason=READY_FOR_CUSTOMER_INPUT timestamp={datetime.now(timezone.utc).isoformat()}")
    await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Ready for customer input")
    logger.info("[STT] ready_for_next_utterance=true")

    # 4. State variables & helper for Authoritative Multi-Turn Listening Resets
    in_q: asyncio.Queue = session["inbound_queue"]
    SILENCE_DURATION_SEC = 0.35  # 350ms natural silence detects end of turn (fast sub-second conversational turn-taking)
    MAX_RECORD_SEC = 8.0         # Max customer speech duration per turn

    collected_pcm: list[int] = []
    pre_roll_chunks = deque(maxlen=12)  # Rolling 240ms pre-roll buffer to preserve consonants
    is_speaking = False
    silence_start_time: Optional[float] = None
    speech_start_time: Optional[float] = None
    barge_trigger_count = 0
    vad_started_logged = False
    last_processed_customer_transcript = ""
    noise_floor = 35.0  # Per-call adaptive ambient noise floor (telephony baseline)
    recent_ai_utterances: list[str] = [opening_line] if opening_line else []
    echo_guard_until: float = 0.0

    def reset_vad_state():
        nonlocal is_speaking, silence_start_time, speech_start_time, barge_trigger_count, vad_started_logged
        is_speaking = False
        silence_start_time = None
        speech_start_time = None
        barge_trigger_count = 0
        vad_started_logged = False
        collected_pcm.clear()
        pre_roll_chunks.clear()

    async def prepare_customer_listening(t_id: int):
        nonlocal is_speaking, silence_start_time, speech_start_time, barge_trigger_count, collected_pcm, vad_started_logged, last_processed_customer_transcript, noise_floor, echo_guard_until
        session["ai_speaking"] = False
        last_processed_customer_transcript = ""
        # Clear any leftover AI RTP TX queues to guarantee clean transition to customer listening
        if session.get("ai_rtp_tx_queue"):
            session["ai_rtp_tx_queue"].clear()
        if session.get("rtp_outbound_queue"):
            session["rtp_outbound_queue"].clear()

        # Guard against acoustic reflection from network round-trip & Asterisk jitter buffer (200ms)
        echo_guard_until = time.time() + 0.20

        # Drain inbound queue ONLY when bot has just finished speaking its own greeting/response
        purged_count = 0
        while not in_q.empty():
            try:
                in_q.get_nowait()
                purged_count += 1
            except Exception:
                break
        reset_vad_state()
        noise_floor = 35.0

        logger.info(f"[VAD] reset (purged {purged_count} stale audio chunks)")
        logger.info(f"[VAD_RESET] call_id={call_id} turn_id={t_id} reason=Turn state reset for customer listening")
        logger.info(f"[CUSTOMER_LISTENING_START] call_id={call_id} turn_id={t_id}")
        logger.info(f"[CALL_STATE] LISTENING_TO_CUSTOMER call_id={call_id} turn_id={t_id}")
        await broadcast_state_change(call_id, t_id, "LISTENING_FOR_CUSTOMER", "Listening for customer speech on RTP stream")
        logger.info("[STT] ready_for_next_utterance=true")

    # Authoritatively initialize clean listening state after opening greeting
    await prepare_customer_listening(turn_id)

    # Continuous Multi-Turn Listening & Reasoning Loop
    rx_packet_count = 0

    while session.get("running"):
        try:
            chunk = await asyncio.wait_for(in_q.get(), timeout=0.05)
        except asyncio.TimeoutError:
            chunk = None
        except Exception:
            break

        now = time.time()
        is_bot_speaking = bool(session.get("ai_speaking", False))
        is_in_echo_guard = (now < echo_guard_until)

        if is_bot_speaking:
            current_threshold = 1600.0
            continuation_threshold = 1200.0
        elif is_in_echo_guard:
            current_threshold = 2000.0
            continuation_threshold = 1600.0
        else:
            current_threshold = max(65.0, min(220.0, noise_floor + 30.0))
            continuation_threshold = max(45.0, min(150.0, noise_floor + 18.0))

        if chunk:
            rx_packet_count += 1
            q_depth = in_q.qsize()
            if rx_packet_count % 100 == 0 or rx_packet_count == 1 or q_depth > 100:
                logger.info(f"[PHONE_RTP_RECV] [VAD_READ] Customer audio packet read call_id={call_id} turn_id={turn_id} packets={rx_packet_count} bytes={len(chunk)} queue_depth={q_depth}")

            # Decode mu-law to 16-bit PCM
            pcm_chunk = [ulaw2pcm(b) for b in chunk]
            sum_sq = sum(s * s for s in pcm_chunk)
            rms = math.sqrt(sum_sq / max(1, len(pcm_chunk)))

            # If within echo guard window (right after AI finished speaking), drain packet and discard
            if is_in_echo_guard:
                pre_roll_chunks.clear()
                continue

            # Update per-call noise floor ONLY during ambient/silence frames when bot is not speaking and outside echo guard
            if not is_bot_speaking and not is_speaking and rms < current_threshold:
                noise_floor = max(15.0, min(80.0, noise_floor * 0.95 + rms * 0.05))

            if rx_packet_count % 100 == 0 or rx_packet_count == 1:
                logger.info(f"[VAD_PEEK] call_id={call_id} RMS={rms:.1f} noise_floor={noise_floor:.1f} threshold={current_threshold:.1f} is_speaking={is_speaking} is_bot_speaking={is_bot_speaking}")

            # Handle Barge-in interruption while bot is talking
            if is_bot_speaking:
                pre_roll_chunks.clear()
                # Genuine human caller speech must significantly exceed reflected echo (> 1600) and sustain for >= 6 frames (120ms)
                if rms > 1600:
                    barge_trigger_count += 1
                    if barge_trigger_count >= 6:
                        logger.info(f"[BARGE_IN] Genuine loud caller interruption detected (RMS={rms:.1f}, count={barge_trigger_count}), interrupting AI speech!")
                        if "ai_rtp_tx_queue" in session:
                            session["ai_rtp_tx_queue"].clear()
                        if "rtp_outbound_queue" in session:
                            session["rtp_outbound_queue"].clear()
                        session["ai_speaking"] = False
                        is_bot_speaking = False
                        barge_trigger_count = 0
                else:
                    barge_trigger_count = max(0, barge_trigger_count - 1)
                # While bot is speaking, do not collect frames as customer speech
                continue

            if not is_speaking:
                pre_roll_chunks.append(pcm_chunk)
                if rms > current_threshold and not is_bot_speaking and not is_in_echo_guard:
                    is_speaking = True
                    speech_start_time = now
                    silence_start_time = None
                    collected_pcm = []
                    for c in pre_roll_chunks:
                        collected_pcm.extend(c)
                    collected_pcm.extend(pcm_chunk)
                    if not vad_started_logged:
                        dur_ms = int((now - speech_start_time) * 1000)
                        logger.info(f"[TURN {turn_id}] customer_audio_started")
                        logger.info(f"[VAD][PHONE_SPEECH] customer speech detected call_id={call_id} turn_id={turn_id} RMS={rms:.1f}")
                        logger.info(f"[VAD_START] call_id={call_id} turn_id={turn_id} RMS={rms:.1f} threshold={current_threshold:.1f}")
                        logger.info(f"[STATE] LISTENING_FOR_CUSTOMER -> CUSTOMER_SPEAKING")
                        logger.info(f"[CALL_STATE] call_id={call_id} previous=LISTENING_FOR_CUSTOMER new=CUSTOMER_SPEAKING reason=VAD_SPEECH_DETECTED timestamp={datetime.now(timezone.utc).isoformat()}")
                        await broadcast_state_change(call_id, turn_id, "CUSTOMER_SPEAKING", "Customer started speaking")
                        vad_started_logged = True
            else:
                collected_pcm.extend(pcm_chunk)
                if rms > continuation_threshold:
                    silence_start_time = None
                else:
                    if silence_start_time is None:
                        silence_start_time = now
        else:
            if not is_speaking:
                continue

        # Turn completion check
        turn_finished = False
        if is_speaking:
            if silence_start_time and (now - silence_start_time >= SILENCE_DURATION_SEC):
                turn_finished = True
            elif speech_start_time and (now - speech_start_time >= MAX_RECORD_SEC):
                turn_finished = True

        if turn_finished:
            audio_dur_ms = int((len(collected_pcm) / 8)) if collected_pcm else 0
            logger.info(f"[VAD] silence_started")
            logger.info(f"[VAD] Customer speech ended audio_ms={audio_dur_ms}")
            logger.info(f"[VAD_END] call_id={call_id} turn_id={turn_id} audio_ms={audio_dur_ms} samples={len(collected_pcm)}")

            pcm_to_transcribe = collected_pcm[:]
            collected_pcm = []
            pre_roll_chunks.clear()
            is_speaking = False
            silence_start_time = None
            speech_start_time = None
            vad_started_logged = False
            logger.info(f"[VAD] reset")

            # Check if collected speech meets minimum audio duration (>= 1200 samples = 150ms)
            if len(pcm_to_transcribe) < 1200:
                logger.debug(f"[SIP AI BOT] Discarded short audio burst ({len(pcm_to_transcribe)} samples < 150ms)")
                await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Short audio burst discarded, listening for customer input")
                reset_vad_state()
                continue

            max_amp = max(abs(s) for s in pcm_to_transcribe) if pcm_to_transcribe else 0
            if max_amp < 60:
                logger.debug(f"[SIP AI BOT] Discarded ambient line noise (max_amp={max_amp})")
                await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Ambient noise discarded, listening for customer input")
                reset_vad_state()
                continue

            logger.info(f"[STATE] CUSTOMER_SPEAKING -> PROCESSING_STT")
            logger.info(f"[CALL_STATE] call_id={call_id} previous=CUSTOMER_SPEAKING new=PROCESSING_STT reason=VAD_UTTERANCE_COMPLETED timestamp={datetime.now(timezone.utc).isoformat()}")
            await broadcast_state_change(call_id, turn_id, "PROCESSING_STT", "VAD ended speech, executing STT")

            # 1. STT Recognition
            logger.info(f"[STT] Processing customer audio call_id={call_id} turn_id={turn_id}")
            stt_dur_sec = len(pcm_to_transcribe) / 8000.0 if pcm_to_transcribe else 0.0
            stt_pkts = len(pcm_to_transcribe) // 160 if pcm_to_transcribe else 0
            logger.info(f"[STT_START] call_id={call_id} turn_id={turn_id} bytes={len(pcm_to_transcribe)*2} duration_sec={stt_dur_sec:.2f}")
            logger.info(
                f"[STT_RX]\n"
                f"codec=PCMU\n"
                f"payload_type=0\n"
                f"packet_count={stt_pkts}\n"
                f"bytes={len(pcm_to_transcribe) * 2}\n"
                f"sample_rate=8000\n"
                f"channels=1\n"
                f"audio_duration={stt_dur_sec:.2f}"
            )
            wav_bytes = build_wav_from_pcm(pcm_to_transcribe, sample_rate=8000)
            caller_text = ""
            stt_start_ts = time.time()
            stt_start_iso = datetime.now(timezone.utc).isoformat()
            logger.info(
                f"[STT_STAGE_START] call_id={call_id} turn_id={turn_id} provider=DEEPGRAM "
                f"format=WAV sample_rate=8000 channels=1 sample_width=2 samples={len(pcm_to_transcribe)} "
                f"bytes={len(wav_bytes)} duration_sec={stt_dur_sec:.2f} timestamp={stt_start_iso}"
            )
            try:
                from app.services.stt_service import transcribe_audio
                logger.info(f"[STT][PHONE_AUDIO] audio segment sent to Deepgram call_id={call_id} turn_id={turn_id} bytes={len(wav_bytes)}")
                cfg_lang = getattr(agent, "language", None) or (camp_voice_cfg.get("language") if isinstance(camp_voice_cfg, dict) else None) or "en-IN"
                if cfg_lang in ("en", "english"):
                    stt_lang = "en-IN"
                elif cfg_lang in ("ta", "tamil"):
                    stt_lang = "ta-IN"
                elif cfg_lang in ("hi", "hindi"):
                    stt_lang = "hi-IN"
                else:
                    stt_lang = cfg_lang if "-" in str(cfg_lang) else f"{cfg_lang}-IN"
                caller_text = await transcribe_audio(wav_bytes, mimetype="audio/wav", language_code=stt_lang)
                stt_end_ts = time.time()
                stt_elapsed_ms = int((stt_end_ts - stt_start_ts) * 1000)
                logger.info(f"[STT][RESULT] transcript received call_id={call_id} turn_id={turn_id} transcript=\"{caller_text}\" latency_ms={stt_elapsed_ms}")
                logger.info(
                    f"[STT_STAGE_END] call_id={call_id} turn_id={turn_id} transcript=\"{caller_text}\" "
                    f"latency_ms={stt_elapsed_ms} timestamp={datetime.now(timezone.utc).isoformat()}"
                )
                if caller_text and "[stub-transcript]" in caller_text:
                    caller_text = ""
            except Exception as stt_err:
                logger.error(f"[STT_ERROR] call_id={call_id} turn_id={turn_id} error=\"{stt_err}\"")
                logger.warning(f"[SIP AI BOT] STT recognition notice: {stt_err}")

            clean_caller = (caller_text or "").strip()
            logger.info(f"[STT] Transcript: \"{clean_caller}\" call_id={call_id} turn_id={turn_id}")
            logger.info(f"[TURN {turn_id}] stt_completed transcript=\"{clean_caller}\"")
            logger.info(f"[STT_FINAL] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\" is_final=true speech_final=true available={bool(clean_caller)}")

            if not clean_caller:
                logger.info(f"[STT_EMPTY] call_id={call_id} turn_id={turn_id} duration_sec={stt_dur_sec:.2f} returning_to_listening=true")
                logger.info(f"[STT] No recognizable speech in audio segment (samples={len(pcm_to_transcribe)} dur={stt_dur_sec:.2f}s) call_id={call_id} turn_id={turn_id} - returning to listening")
                await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "No speech recognized, ready for customer input")
                reset_vad_state()
                continue

            if last_processed_customer_transcript and clean_caller.lower() == last_processed_customer_transcript.lower():
                logger.info(f"[STT_DUPLICATE_IGNORED] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\"")
                await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Duplicate utterance ignored, ready for customer input")
                turn_id += 1
                reset_vad_state()
                continue

            # Acoustic Echo / Self-Hearing Protection: Discard transcript if caller audio matches AI's own recent speech
            is_echo_of_ai = False
            clean_lower = clean_caller.lower()
            clean_words = set(re.findall(r'[a-zA-Z0-9]+', clean_lower))
            substantive_words = {w for w in clean_words if len(w) > 2 and w not in {"the", "and", "for", "with", "this", "that", "you", "are", "have", "from", "yes", "yeah", "okay"}}

            for past_ai_text in recent_ai_utterances[-3:]:
                past_lower = past_ai_text.lower()
                if len(clean_lower) >= 6 and clean_lower in past_lower:
                    is_echo_of_ai = True
                    break
                if substantive_words:
                    past_words = set(re.findall(r'[a-zA-Z0-9]+', past_lower))
                    matched = substantive_words.intersection(past_words)
                    if len(matched) >= 2 and (len(matched) / len(substantive_words)) >= 0.65:
                        is_echo_of_ai = True
                        break

            if is_echo_of_ai:
                logger.warning(
                    f"[ECHO_DISCARDED] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\" "
                    f"matched recent AI speech. Discarded to prevent self-hearing loop."
                )
                await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Acoustic reflection filtered, listening for customer input")
                reset_vad_state()
                continue

            last_processed_customer_transcript = clean_caller
            logger.info(f"[AUTO_SUBMIT] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\" source=DEEPGRAM")
            await broadcast_transcript(call_id, "Customer", clean_caller, turn_id=turn_id)

            # 1.5 Natural Speech-Based Human Voice Agent Transfer Intent Detection
            from app.services.human_handoff_intent import HumanHandoffIntentDetector
            transfer_intent = HumanHandoffIntentDetector.detect(clean_caller)

            is_transfer_request = (
                transfer_intent.has_intent
                and transfer_intent.confidence >= 0.70
                and not session.get("is_transferred")
                and not session.get("transfer_initiated")
            )

            if is_transfer_request:
                session["transfer_initiated"] = True
                logger.info(
                    f"[SPEECH_TRANSFER_DETECTED] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\" "
                    f"lang={transfer_intent.language} phrase=\"{transfer_intent.matched_phrase}\" conf={transfer_intent.confidence}"
                )
                await broadcast_state_change(call_id, turn_id, "TRANSFER_REQUESTED", "Speech transfer intent detected, evaluating Voice Agent availability")

                transfer_success = False
                fallback_speech = None
                assigned_expert_name = None

                try:
                    from app.services.call_transfer_service import CallTransferService
                    from app.services.lead_assignment_service import LeadAssignmentService

                    async with AsyncSessionLocal() as trans_db:
                        call_obj = await trans_db.get(Call, call_id)
                        # Ensure call is active and not terminal
                        if call_obj and call_obj.status in ("COMPLETED", "FAILED", "NO_ANSWER", "BUSY", "CANCELED"):
                            logger.warning(f"[SPEECH_TRANSFER] Call {call_id} in terminal state ({call_obj.status}). Transfer aborted.")
                            break

                        # 1. Resolve assigned Voice Agent from Campaign or Lead
                        assigned_va_id = (
                            getattr(campaign, "voice_agent_id", None)
                            or (getattr(lead, "assigned_expert_id", None) if lead else None)
                        )
                        assigned_expert_name = None

                        # 2. Assign lead to the assigned voice agent and dispatch real-time LEAD_ASSIGNED event
                        if lead:
                            try:
                                la_res = await LeadAssignmentService.transfer_lead_to_agent(
                                    db=trans_db,
                                    business_id=call.business_id or (agent.business_id if agent else "default"),
                                    lead_id=lead.id,
                                    campaign_id=call.campaign_id or (campaign.id if campaign else None),
                                    ai_agent_name=agent.name if agent else "CallZenza AI Agent",
                                    qualification_stage="Voice Transfer Handoff",
                                    transcript=(call.transcript or "") + f"\nCustomer: {clean_caller}",
                                    call_id=call_id,
                                    notes=f"Customer voice request: '{clean_caller}'",
                                )
                                if la_res and la_res.get("assigned_expert"):
                                    exp_info = la_res.get("assigned_expert")
                                    assigned_expert_name = exp_info.get("name")
                                    if not assigned_va_id:
                                        assigned_va_id = exp_info.get("id") or exp_info.get("user_id")
                            except Exception as la_err:
                                logger.warning(f"[SPEECH_TRANSFER] Lead assignment notice: {la_err}")

                        # 3. Telephony warm transfer to the assigned agent
                        trans_res = await CallTransferService.initiate_warm_transfer(
                            call_id=call_id,
                            session_id=session.get("session_id") or call_id,
                            business_id=call.business_id or (agent.business_id if agent else "default"),
                            campaign_id=call.campaign_id or (campaign.id if campaign else None),
                            lead_id=call.lead_id or (lead.id if lead else None),
                            customer_name=lead.first_name if lead else (call.client_name or "Customer"),
                            customer_phone=lead.phone if lead else (call.client_phone or ""),
                            language=transfer_intent.language,
                            notes=f"Speech transfer intent: '{clean_caller}'",
                            transcript=(call.transcript or "") + f"\nCustomer: {clean_caller}",
                            target_agent_id=assigned_va_id,
                            db=trans_db,
                        )

                        if trans_res.get("is_fallback") or trans_res.get("status") == "FAILED":
                            fallback_speech = trans_res.get("message") or (
                                "All our human specialists are currently assisting other customers. "
                                "I will arrange a priority callback for you, or we can continue our conversation right now."
                            )
                        else:
                            transfer_success = True
                            assigned_expert_name = trans_res.get("assigned_agent_name") or assigned_expert_name or "our specialist"
                except Exception as trans_err:
                    logger.exception(f"[SPEECH_TRANSFER_ERROR] Failed initiating warm transfer: {trans_err}")
                    fallback_speech = "I apologize, all our specialists are currently busy. How else may I assist you?"

                if not transfer_success:
                    logger.info(f"[SPEECH_TRANSFER_FALLBACK] No agent available. Informing customer and continuing AI: \"{fallback_speech}\"")
                    session["transfer_initiated"] = False
                    session["ai_speaking"] = True
                    await broadcast_transcript(call_id, "Agent", fallback_speech, turn_id=turn_id)
                    fb_mulaw = await synthesize_text_to_mulaw(
                        fallback_speech,
                        agent=agent,
                        call_id=call_id,
                        campaign=campaign,
                        voice_config=camp_voice_cfg,
                        business_id=biz_id,
                    )
                    if fb_mulaw:
                        fanout_ai_audio(fb_mulaw, turn_id)
                        ai_tx_q = session.get("ai_rtp_tx_queue")
                        while session.get("running") and ai_tx_q is not None and len(ai_tx_q) > 0:
                            await asyncio.sleep(0.02)
                        await asyncio.sleep(0.35)
                    session["ai_speaking"] = False
                    turn_id += 1
                    await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Ready for customer input")
                    await prepare_customer_listening(turn_id)
                    continue

                # Spoken acknowledgment:
                hold_speech = "Certainly, please hold while I connect you to an available agent."
                if transfer_intent.language == "ta":
                    hold_speech = "நிச்சயமாக, எங்கள் முகவருடன் இணைக்கும் வரை தயவுசெய்து காத்திருக்கவும்."
                elif transfer_intent.language == "hi":
                    hold_speech = "ज़रूर, कृपया लाइन पर बने रहें, मैं आपको एक एजेंट से कनेक्ट कर रहा हूँ।"

                logger.info(f"[SPEECH_TRANSFER_SUCCESS] Reserved Voice Agent {assigned_expert_name}. Speaking hold acknowledgement: \"{hold_speech}\"")
                session["ai_speaking"] = True
                await broadcast_transcript(call_id, "Agent", hold_speech, turn_id=turn_id)
                await broadcast_state_change(call_id, turn_id, "AI_SPEAKING", "Connecting to human Voice Agent")

                hold_mulaw = await synthesize_text_to_mulaw(
                    hold_speech,
                    agent=agent,
                    call_id=call_id,
                    campaign=campaign,
                    voice_config=camp_voice_cfg,
                    business_id=biz_id,
                )
                if hold_mulaw:
                    fanout_ai_audio(hold_mulaw, turn_id)
                    ai_tx_q = session.get("ai_rtp_tx_queue")
                    while session.get("running") and ai_tx_q is not None and len(ai_tx_q) > 0:
                        await asyncio.sleep(0.02)
                    await asyncio.sleep(0.35)

                session["ai_speaking"] = False
                session["is_transferred"] = True
                session["transfer_status"] = "RINGING"
                session["is_voice_agent_call"] = True
                session["assigned_expert_name"] = assigned_expert_name

                # Permanently pause AI bot so it does NOT speak over the human agent
                pause_sip_ai_bot(call_id)
                await broadcast_state_change(call_id, turn_id, "WITH_HUMAN_AGENT", f"Transferred to {assigned_expert_name}")

                # Yield to Voice Agent until call completion
                while session.get("running") and not session.get("stopped"):
                    await asyncio.sleep(1.0)
                break

            # 2. LLM / Gemini Reasoning
            logger.info(f"[VOICE_INPUT] Sending transcript to AgentEngine call_id={call_id} transcript=\"{clean_caller}\"")
            logger.info(f"[STATE] PROCESSING_STT -> PROCESSING_AGENT")
            logger.info(f"[CALL_STATE] call_id={call_id} previous=PROCESSING_STT new=PROCESSING_AGENT reason=STT_TRANSCRIPT_RECEIVED timestamp={datetime.now(timezone.utc).isoformat()}")
            await broadcast_state_change(call_id, turn_id, "PROCESSING_AGENT", "Executing Gemini RAG reasoning")
            logger.info(f"[AI_PROCESSING_START] call_id={call_id} turn_id={turn_id} transcript=\"{clean_caller}\"")
            logger.info(f"[AGENT_ENGINE_TRIGGER] call_id={call_id} source=STT_AUTO_SUBMIT")
            logger.info(f"[GEMINI_START] call_id={call_id} turn_id={turn_id}")
            logger.info(f"[RAG_RETRIEVAL] call_id={call_id} turn_id={turn_id}")
            logger.info(f"[PROCESSING_START] call_id={call_id} turn_id={turn_id}")
            t_llm_start = time.perf_counter()
            logger.info(f"[LLM_STAGE_START] call_id={call_id} turn_id={turn_id} prompt=\"{clean_caller}\" timestamp={datetime.now(timezone.utc).isoformat()}")
            try:
                async with AsyncSessionLocal() as db:
                    lead_row = await db.get(Lead, lead.id) if lead else None
                    conv_row = await db.get(Conversation, conversation.id)
                    agent_row = await db.get(AIAgent, agent.id) if agent else None
                    camp_row = await db.get(Campaign, campaign.id) if campaign else None
                    if agent_row:
                        agent = agent_row

                    rag_result = await process_incoming_message(
                        db=db,
                        lead=lead_row,
                        conversation=conv_row,
                        agent=agent_row,
                        content=clean_caller,
                        campaign=camp_row,
                    )

                    # Check DND intent
                    from app.services.dnd_service import detect_dnd_intent, add_to_dnd
                    is_dnd_turn = detect_dnd_intent(clean_caller)
                    if is_dnd_turn and lead_row and lead_row.phone:
                        await add_to_dnd(
                            db=db,
                            phone=lead_row.phone,
                            business_id=lead_row.business_id,
                            lead_id=lead_row.id,
                            campaign_id=camp_row.id if camp_row else None,
                            call_id=call_id,
                            reason="Customer requested Do Not Call during SIP call",
                        )
                        if is_dnd_turn and not rag_result.get("ai_message"):
                            ai_reply = "I apologize for any inconvenience. I have added your number to our Do Not Call list and you will not receive any further calls from us. Thank you."

                    await db.commit()

                llm_elapsed_ms = int((time.perf_counter() - t_llm_start) * 1000)
                ai_reply = rag_result["ai_message"].content if rag_result.get("ai_message") else ("I apologize for any inconvenience. I have added your number to our Do Not Call list. Thank you." if is_dnd_turn else "Thank you. Let me share the details with you.")
                logger.info(f"[LLM_STAGE_END] call_id={call_id} turn_id={turn_id} latency_ms={llm_elapsed_ms} timestamp={datetime.now(timezone.utc).isoformat()}")
                
                retrieved_flag = rag_result.get("rag_retrieved", False)
                rag_len = rag_result.get("rag_context_length", 0)
                rag_prev = rag_result.get("rag_context_preview", "None")
                script_matched = rag_result.get("script_matched", False)
                script_q = rag_result.get("intent", "general")
                guard_passed = rag_result.get("response_guard_passed", True)
                guard_reason = rag_result.get("response_guard_reason", "ok")

                logger.info(
                    f"\n[CALL_FLOW]\nstate=PROCESSING_AGENT\n"
                    f"[STT_RESULT]\ntranscript=\"{clean_caller}\"\n"
                    f"[RAG_CONTEXT]\nretrieved={retrieved_flag} length={rag_len} preview=\"{rag_prev}\"\n"
                    f"[SCRIPT_MATCH]\nmatched={script_matched} question=\"{script_q}\"\n"
                    f"[LLM_RESPONSE]\ncall_id={call_id} text=\"{ai_reply}\" length={len(ai_reply)}\n"
                    f"[RESPONSE_GUARD]\npassed={guard_passed} reason=\"{guard_reason}\"\n"
                    f"[TTS_INPUT]\ntext=\"{ai_reply}\" length={len(ai_reply)}"
                )

                logger.info(f"[AI_RESPONSE] Response generated text=\"{ai_reply}\" call_id={call_id} turn_id={turn_id}")
                logger.info(f"[AI_PROCESSING_COMPLETE] call_id={call_id} turn_id={turn_id} reply=\"{ai_reply}\" rag_retrieved={retrieved_flag}")
                logger.info(f"[TURN {turn_id}] agent_completed response=\"{ai_reply}\"")
                logger.info(f"[LLM_RESPONSE] call_id={call_id} turn_id={turn_id} response=\"{ai_reply}\"")
                logger.info(f"[GEMINI_RESPONSE] call_id={call_id} turn_id={turn_id}")
                recent_ai_utterances.append(ai_reply)
                await broadcast_transcript(call_id, "Agent", ai_reply, turn_id=turn_id)
            except Exception as gem_err:
                logger.error(f"[VOICE_PIPELINE_ERROR] call_id={call_id} turn_id={turn_id} reason=GEMINI_RESPONSE_MISSING detail=\"{gem_err}\"")
                ai_reply = "Thank you for your response. How else may I assist you?"
                recent_ai_utterances.append(ai_reply)

            # Save transcript in DB
            async def _update_db_transcript(c_text, r_text):
                async with AsyncSessionLocal() as db:
                    call_obj = await db.get(Call, call_id)
                    if call_obj:
                        call_obj.transcript = (call_obj.transcript or "") + f"Customer: {c_text}\nAgent: {r_text}\n"
                        call_obj.turn_count = (call_obj.turn_count or 0) + 1
                        db.add(call_obj)
                        try:
                            from app.db.models import CallMessage
                            if c_text:
                                db.add(CallMessage(call_id=call_obj.id, sender_type="CUSTOMER", content=c_text))
                            if r_text:
                                db.add(CallMessage(call_id=call_obj.id, sender_type="AGENT", content=r_text))
                        except Exception as msg_err:
                            logger.debug(f"Failed to save CallMessage: {msg_err}")
                        await db.commit()
            asyncio.create_task(_update_db_transcript(clean_caller, ai_reply))

            # Check if supervisor is actively barging in - yield AI speech immediately
            if session.get("supervisor_barging"):
                logger.info(f"[SIP AI BOT] Supervisor barge-in active on call {call_id}; yielding AI speech.")
                session["ai_speaking"] = False
                turn_id += 1
                await prepare_customer_listening(turn_id)
                continue

            # 3. TTS Generation & Enqueueing
            logger.info(f"[TTS] Speaking response text=\"{ai_reply}\" call_id={call_id} turn_id={turn_id}")
            logger.info(f"[TTS_TRIGGER] call_id={call_id} turn_id={turn_id} text=\"{ai_reply}\"")
            logger.info(f"[STATE] PROCESSING_AGENT -> AI_SPEAKING")
            logger.info(f"[CALL_STATE] call_id={call_id} previous=PROCESSING_AGENT new=AI_SPEAKING reason=AGENT_RESPONSE_READY timestamp={datetime.now(timezone.utc).isoformat()}")
            await broadcast_state_change(call_id, turn_id, "AI_SPEAKING", "Synthesizing and streaming response TTS")
            session["ai_speaking"] = True
            logger.info(f"[AI_AUDIO_START] call_id={call_id} turn_id={turn_id}")
            logger.info(f"[TTS] START call_id={call_id} turn_id={turn_id}")
            logger.info(f"[TTS_START] call_id={call_id} turn_id={turn_id}")
            
            # Clear stale audio from previous turns before queuing new response
            if session.get("ai_rtp_tx_queue"):
                session["ai_rtp_tx_queue"].clear()
            if session.get("rtp_outbound_queue"):
                session["rtp_outbound_queue"].clear()

            logger.info(f"[RTP_TX_FIRST] call_id={call_id} turn_id={turn_id}")
            tts_generated_any = False
            t_tts_start = time.perf_counter()
            logger.info(f"[TTS_STAGE_START] call_id={call_id} turn_id={turn_id} text=\"{ai_reply[:60]}\" timestamp={datetime.now(timezone.utc).isoformat()}")

            raw_sentences = [s.strip() for s in re.split(r'(?<=[.?!])\s+', ai_reply) if s.strip()]
            if not raw_sentences:
                raw_sentences = [ai_reply]

            total_ai_mulaw_bytes = 0
            for s_idx, s_text in enumerate(raw_sentences):
                if session.get("supervisor_barging") or not session.get("running"):
                    break
                s_t0 = time.perf_counter()
                chunk_mulaw = await synthesize_text_to_mulaw(
                    s_text,
                    agent=agent,
                    call_id=call_id,
                    campaign=campaign,
                    voice_config=camp_voice_cfg,
                    business_id=biz_id,
                )
                if chunk_mulaw and not session.get("supervisor_barging"):
                    tts_generated_any = True
                    s_lat_ms = int((time.perf_counter() - s_t0) * 1000)
                    total_ai_mulaw_bytes += len(chunk_mulaw)
                    logger.info(f"[TTS_SENTENCE_READY] call_id={call_id} turn_id={turn_id} sentence_idx={s_idx+1}/{len(raw_sentences)} bytes={len(chunk_mulaw)} latency_ms={s_lat_ms}")
                    logger.info(f"[TTS_PCM_READY] call_id={call_id} turn_id={turn_id} bytes={len(chunk_mulaw)}")
                    logger.info(f"[AI_AUDIO_TX] call_id={call_id} turn_id={turn_id} bytes={len(chunk_mulaw)}")
                    fanout_ai_audio(chunk_mulaw, turn_id)

            tts_total_ms = int((time.perf_counter() - t_tts_start) * 1000)
            logger.info(f"[TTS_STAGE_END] call_id={call_id} turn_id={turn_id} total_bytes={total_ai_mulaw_bytes} latency_ms={tts_total_ms} timestamp={datetime.now(timezone.utc).isoformat()}")
            logger.info(f"[AI_RESPONSE_PLAYBACK_START] call_id={call_id} turn_id={turn_id} bytes={total_ai_mulaw_bytes}")

            if not tts_generated_any and not session.get("supervisor_barging"):
                logger.error(f"[VOICE_PIPELINE_ERROR] call_id={call_id} turn_id={turn_id} reason=TTS_GENERATION_FAILED")

            logger.info(f"[RTP_TX_LAST] call_id={call_id} turn_id={turn_id}")

            # Wait for dedicated outbound AI audio queue to completely drain before listening to next turn
            ai_tx_q = session.get("ai_rtp_tx_queue")
            while session.get("running") and ai_tx_q is not None and len(ai_tx_q) > 0:
                if session.get("supervisor_barging"):
                    ai_tx_q.clear()
                    break
                await asyncio.sleep(0.02)
            # Allow physical UDP socket buffer playback to complete before switching state
            await asyncio.sleep(0.35)
            session["ai_speaking"] = False
            logger.info(f"[AI_AUDIO_END] call_id={call_id} turn_id={turn_id}")

            logger.info(f"[TTS] COMPLETE call_id={call_id} turn_id={turn_id}")
            logger.info(f"[TURN {turn_id}] tts_completed")
            logger.info(f"[RTP_PLAYBACK_COMPLETE] call_id={call_id} turn_id={turn_id}")
            logger.info(f"[AI_AUDIO_PLAYBACK_COMPLETE] call_id={call_id} turn_id={turn_id}")
            logger.info(f"[AI_RESPONSE_PLAYBACK_COMPLETE] call_id={call_id} turn_id={turn_id} status=SUCCESS")

            if is_dnd_turn or rag_result.get("end_call") or rag_result.get("terminal_hangup"):
                logger.info(f"[SIP AI BOT] Terminal condition met (dnd={is_dnd_turn}, end_call={rag_result.get('end_call')}). Executing Asterisk hangup for call {call_id}.")
                from app.services.voice_service import hangup_sip_call
                try:
                    hangup_sip_call(call_id)
                except Exception as h_err:
                    logger.warning(f"[SIP AI BOT] Hangup notice: {h_err}")
                break

            # Monotonically increment turn_id and reset turn state for next customer turn
            logger.info(f"[STATE] AI_SPEAKING -> RETURNING_TO_LISTENING")
            logger.info(f"[CALL_STATE] call_id={call_id} previous=AI_SPEAKING new=RETURNING_TO_LISTENING reason=TTS_COMPLETED timestamp={datetime.now(timezone.utc).isoformat()}")
            await broadcast_state_change(call_id, turn_id, "RETURNING_TO_LISTENING", "Response TTS completed")

            logger.info(f"[STATE] RETURNING_TO_LISTENING -> LISTENING_FOR_CUSTOMER")
            logger.info(f"[CALL_STATE] LISTENING_TO_CUSTOMER call_id={call_id} previous=RETURNING_TO_LISTENING new=LISTENING_FOR_CUSTOMER reason=READY_FOR_NEXT_CUSTOMER_INPUT timestamp={datetime.now(timezone.utc).isoformat()}")
            await broadcast_state_change(call_id, turn_id, "LISTENING_FOR_CUSTOMER", "Ready for next customer input")
            logger.info("[TTS] Complete -> returning to customer listening")
            logger.info("[STT] ready_for_next_utterance=true")

            turn_id += 1
            await prepare_customer_listening(turn_id)

    logger.info(f"[SIP AI BOT] AI Voice Bot loop completed for call {call_id}")

    logger.info(f"[SIP AI BOT] AI Voice Bot loop completed for call {call_id}")


def start_sip_ai_bot(call_id: str, is_inbound: bool = True):
    """Launches the background SIP AI Voice Bot on the given call."""
    if call_id in ACTIVE_SIP_BOT_TASKS and not ACTIVE_SIP_BOT_TASKS[call_id].done():
        logger.info(f"[SIP AI BOT] Bot already running for call {call_id}")
        return

    from app.api.routes.barge_stream import barge_sessions
    sess = barge_sessions.get(call_id) or (barge_sessions.get(call_id.split("@")[0]) if "@" in call_id else None)
    if sess:
        if sess.get("is_voice_agent_call") is True or (sess.get("agent_type") or "").upper() in ("HUMAN", "VOICE", "VOICE_AGENT", "HUMAN_VOICE_AGENT"):
            logger.info(f"[SIP AI BOT] Call {call_id} is marked as HUMAN Voice Agent. AI bot startup prevented.")
            return
        if sess.get("ai_bot_started") and (call_id in ACTIVE_SIP_BOT_TASKS and not ACTIVE_SIP_BOT_TASKS[call_id].done()):
            logger.info(f"[SIP AI BOT] Bot already launched on session for call {call_id}. Skipping.")
            return
        sess["ai_bot_started"] = True

    task = asyncio.create_task(_run_sip_ai_bot(call_id, is_inbound))
    ACTIVE_SIP_BOT_TASKS[call_id] = task
    logger.info(f"[SIP AI BOT] Dispatched background AI Voice Bot task for call {call_id}")


def stop_sip_ai_bot(call_id: str):
    """Cancels and cleans up the background SIP AI Voice Bot for the given call."""
    task = ACTIVE_SIP_BOT_TASKS.pop(call_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"[SIP AI BOT] Stopped AI Voice Bot task for call {call_id}")


def pause_sip_ai_bot(call_id: str):
    """Signals the AI bot to yield speech during live supervisor intervention."""
    try:
        from app.api.routes.barge_stream import barge_sessions
        for cid_key in set([call_id, (call_id.split("@")[0] if "@" in call_id else None)]):
            if cid_key and cid_key in barge_sessions:
                sess = barge_sessions[cid_key]
                already_paused = sess.get("supervisor_barging", False)
                sess["ai_speaking"] = False
                sess["supervisor_barging"] = True
                if "ai_rtp_tx_queue" in sess:
                    q = sess["ai_rtp_tx_queue"]
                    if hasattr(q, "clear"):
                        q.clear()
                    elif hasattr(q, "empty") and hasattr(q, "get_nowait"):
                        while not q.empty():
                            try:
                                q.get_nowait()
                            except Exception:
                                break
                if not already_paused:
                    logger.info(f"[SIP AI BOT] AI Voice Bot speech paused/yielded for call {call_id} due to supervisor barge-in")
    except Exception as e:
        logger.debug(f"[SIP AI BOT] pause_sip_ai_bot error: {e}")


def resume_sip_ai_bot(call_id: str):
    """Resumes AI bot speech capability after supervisor intervention ends."""
    try:
        from app.api.routes.barge_stream import barge_sessions
        for cid_key in set([call_id, (call_id.split("@")[0] if "@" in call_id else None)]):
            if cid_key and cid_key in barge_sessions:
                sess = barge_sessions[cid_key]
                was_paused = sess.get("supervisor_barging", False)
                sess["supervisor_barging"] = False
                if was_paused:
                    logger.info(f"[SIP AI BOT] AI Voice Bot speech resumed for call {call_id} after supervisor barge-in ended")
    except Exception as e:
        logger.debug(f"[SIP AI BOT] resume_sip_ai_bot error: {e}")

