"""
Module 15/20 — Text-to-Speech (ElevenLabs).

This is the real integration point that replaces Twilio's built-in <Say>
voice: synthesize_for_call() calls the ElevenLabs API using the calling
agent's configured voice (voice_id, stability, similarity_boost, model),
writes the resulting mp3 to a locally-served static folder, and returns a
public URL that Twilio's <Play> verb can fetch. If no ELEVENLABS_API_KEY is
configured, it returns None and the voice routes fall back to Twilio's
built-in <Say> automatically — so the call flow still works with zero
external TTS credentials, same pattern as the mock voice provider.
"""
from __future__ import annotations

import hashlib
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from app.core.config import get_settings

settings = get_settings()

AUDIO_DIR = Path(__file__).resolve().parent.parent / "static" / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)


DEFAULT_PREMADE_VOICE = "EXAVITQu4vr4xnSDxMaL"  # Bella / pre-made voice compatible with free and paid tiers

EMOTION_ELEVENLABS_SETTINGS = {
    "happy": {"stability": 0.18, "similarity_boost": 0.85, "style": 0.80},
    "sad": {"stability": 0.85, "similarity_boost": 0.55, "style": 0.10},
    "fearful": {"stability": 0.15, "similarity_boost": 0.70, "style": 0.65},
    "disgusted": {"stability": 0.55, "similarity_boost": 0.65, "style": 0.40},
    "surprised": {"stability": 0.12, "similarity_boost": 0.90, "style": 0.85},
    "neutral": {"stability": 0.55, "similarity_boost": 0.75, "style": 0.05},
    "fluent": {"stability": 0.35, "similarity_boost": 0.85, "style": 0.30},
}

EMOTION_SARVAM_SETTINGS = {
    "happy": {"pitch": 0.30, "pace": 1.08},
    "sad": {"pitch": -0.25, "pace": 0.88},
    "fearful": {"pitch": 0.30, "pace": 1.15},
    "disgusted": {"pitch": -0.15, "pace": 0.92},
    "surprised": {"pitch": 0.40, "pace": 1.10},
    "neutral": {"pitch": 0.0, "pace": 1.0},
    "fluent": {"pitch": 0.05, "pace": 1.05},
}


def generate_mp3_silence(duration_sec: float) -> bytes:
    """Generates standard 128kbps 44.1kHz MP3 silence frames."""
    # 1152 samples / 44100 = ~0.026122 seconds per frame
    frame = b"\xff\xfb\x90\x64" + b"\x00" * 413
    num_frames = max(1, int(round(duration_sec / (1152.0 / 44100.0))))
    return frame * num_frames


def clean_markdown_for_speech(text: str) -> str:
    """Strips tags, emotion markers, internal script labels, headers, and markdown for clean audio speech."""
    if not text:
        return ""
    try:
        from app.services.ai.response_sanitizer import ResponseSanitizer
        return ResponseSanitizer.clean_customer_facing_text(text, cut_off_script_branches=True)
    except Exception:
        import re
        t = re.sub(r'<[^>]+>', ' ', text)
        t = re.sub(r'[*_#`~]', '', t)
        return ' '.join(t.split()).strip()



async def synthesize_speech(
    text: str,
    voice_id: str,
    stability: float = 0.5,
    similarity_boost: float = 0.75,
    style: float = 0.0,
    model_id: str = "eleven_turbo_v2_5",
    output_format: str = "mp3_44100_128",
    api_key: Optional[str] = None,
    speed: float = 1.0,
    use_speaker_boost: Optional[bool] = None,
) -> bytes:
    """Raw ElevenLabs call — returns audio bytes (mp3 or pcm), or b"" if no API key configured."""
    effective_api_key = (api_key or settings.elevenlabs_api_key or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not effective_api_key:
        return b""

    from app.utils.tamil_sanitizer import clean_corrupt_tamil
    text = clean_markdown_for_speech(clean_corrupt_tamil(text))

    if not voice_id or not str(voice_id).strip():
        voice_id = DEFAULT_PREMADE_VOICE

    voice_settings: dict = {
        "stability": max(0.0, min(1.0, float(stability))),
        "similarity_boost": max(0.0, min(1.0, float(similarity_boost))),
    }
    if style > 0.0:
        voice_settings["style"] = max(0.0, min(1.0, float(style)))
        voice_settings["use_speaker_boost"] = True
    if use_speaker_boost is not None:
        voice_settings["use_speaker_boost"] = bool(use_speaker_boost)
    if speed is not None and speed != 1.0:
        # ElevenLabs v2 models support speed between 0.7 and 1.2
        voice_settings["speed"] = max(0.7, min(1.2, float(speed)))

    from app.services.http_client import get_shared_client
    client = get_shared_client()

    accept_header = "audio/pcm" if output_format.startswith("pcm") else "audio/mpeg"
    endpoint_url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}?output_format={output_format}"

    resp = await client.post(
        endpoint_url,
        headers={
            "xi-api-key": effective_api_key,
            "Accept": accept_header,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": model_id,
            "voice_settings": voice_settings,
        },
    )
    if resp.status_code != 200:
        try:
            error_data = resp.json()
            detail = error_data.get("detail", {})
            error_msg = (detail.get("message") if isinstance(detail, dict) else str(detail)) or str(error_data)
        except Exception:
            error_msg = resp.text or f"HTTP {resp.status_code}"
        
        # If API key account is restricted from library voices, automatically fall back to pre-made voice
        if ("library voices" in error_msg or "paid_plan_required" in error_msg) and voice_id != DEFAULT_PREMADE_VOICE:
            logger.info(f"Retrying ElevenLabs synthesis with pre-made voice {DEFAULT_PREMADE_VOICE}")
            return await synthesize_speech(
                text=text,
                voice_id=DEFAULT_PREMADE_VOICE,
                stability=stability,
                similarity_boost=similarity_boost,
                style=style,
                model_id=model_id,
                output_format=output_format,
                api_key=effective_api_key,
                speed=speed,
                use_speaker_boost=use_speaker_boost,
            )
        raise RuntimeError(f"ElevenLabs error: {error_msg}")
    return resp.content


_TTS_BYTES_CACHE: Dict[str, bytes] = {}
_SNAPSERVE_DISABLED_UNTIL: float = 0.0


def get_effective_snapserve_key() -> str:
    import os
    key = (getattr(settings, "snapserve_api_key", None) or os.getenv("SNAPSERVE_API_KEY") or "").strip()
    if not key:
        try:
            from dotenv import dotenv_values
            from app.core.config import ENV_FILE
            env_vals = dotenv_values(ENV_FILE)
            key = (env_vals.get("SNAPSERVE_API_KEY") or "").strip()
            if key:
                settings.snapserve_api_key = key
                os.environ["SNAPSERVE_API_KEY"] = key
        except Exception:
            pass
    if not key:
        key = "sk_live_e87a3f9617177dcfcbb27bf5236a38fa00829cb0235cc43e2a9ebfce4454baed"
        settings.snapserve_api_key = key
        os.environ["SNAPSERVE_API_KEY"] = key
    return key


def is_murf_available() -> bool:
    if (getattr(settings, "tts_provider", None) or "").lower().strip() == "snapserve":
        return False
    return bool(get_effective_murf_key())


def mark_murf_quota_exhausted(duration_sec: float = 3600.0) -> None:
    pass


_EDGE_TTS_ENABLED: bool = True


def get_edge_tts_enabled_cached() -> bool:
    """Returns fast in-memory status of Edge TTS."""
    global _EDGE_TTS_ENABLED
    return _EDGE_TTS_ENABLED


async def is_edge_tts_enabled(db: Optional[Any] = None, business_id: Optional[str] = None) -> bool:
    """Checks whether Edge TTS is enabled, querying the database Integration model if db is provided."""
    global _EDGE_TTS_ENABLED
    if db is not None:
        try:
            from app.db.models import Integration
            from sqlalchemy import select
            b_ids = [business_id] if business_id else []
            b_ids.append("00000000-0000-0000-0000-000000000001")
            stmt = select(Integration).where(
                Integration.provider == "edge_tts",
                Integration.business_id.in_(b_ids),
            ).order_by(Integration.created_at.desc())
            integ = (await db.execute(stmt)).scalars().first()
            if integ is not None:
                _EDGE_TTS_ENABLED = bool(integ.is_active)
                return _EDGE_TTS_ENABLED
        except Exception as e:
            logger.debug(f"[EDGE TTS] Integration check notice: {e}")
    return _EDGE_TTS_ENABLED


async def set_edge_tts_enabled(enabled: bool, db: Any, business_id: Optional[str] = None) -> bool:
    """Persists Edge TTS ON/OFF status in database Integration model and updates runtime cache."""
    global _EDGE_TTS_ENABLED
    _EDGE_TTS_ENABLED = bool(enabled)
    try:
        from app.db.models import Integration
        from sqlalchemy import select
        target_bid = business_id or "00000000-0000-0000-0000-000000000001"
        stmt = select(Integration).where(
            Integration.provider == "edge_tts",
            Integration.business_id == target_bid,
        )
        integ = (await db.execute(stmt)).scalars().first()
        if not integ:
            integ = Integration(
                provider="edge_tts",
                business_id=target_bid,
                is_active=enabled,
                config={"enabled": enabled},
            )
            db.add(integ)
        else:
            integ.is_active = enabled
            integ.config = {"enabled": enabled}
            db.add(integ)
        await db.commit()
        await db.refresh(integ)
        logger.info(f"[EDGE TTS] Persisted Edge TTS enabled={enabled} for business_id={target_bid}")
    except Exception as e:
        logger.warning(f"[EDGE TTS] Failed to persist Edge TTS state: {e}")
    return _EDGE_TTS_ENABLED


async def synthesize_edge_speech(
    text: str,
    voice_id: str | None = None,
    rate: float = 0.0,
    pitch: float = 0.0,
    lang: str = "en",
) -> bytes:
    """High-fidelity Neural Edge TTS synthesizer (fast, zero-quota fallback)."""
    if not _EDGE_TTS_ENABLED:
        logger.info("[EDGE TTS] Synthesis skipped: Edge TTS is currently disabled.")
        return b""
    text = clean_markdown_for_speech(text)
    if not text:
        return b""
    is_ta = (lang in ("ta", "ta-IN")) or any("\u0b80" <= c <= "\u0bff" for c in text)
    chosen_voice = voice_id or ("ta-IN-PallaviNeural" if is_ta else "en-US-AriaNeural")
    cache_key = f"edge:{chosen_voice}:{rate}:{pitch}:{text}"
    if cache_key in _TTS_BYTES_CACHE:
        return _TTS_BYTES_CACHE[cache_key]
    try:
        import edge_tts
        comm = edge_tts.Communicate(text, chosen_voice)
        chunks = [c["data"] async for c in comm.stream() if c["type"] == "audio"]
        audio_data = b"".join(chunks)
        if audio_data:
            if len(_TTS_BYTES_CACHE) > 500:
                _TTS_BYTES_CACHE.clear()
            _TTS_BYTES_CACHE[cache_key] = audio_data
            return audio_data
    except Exception as exc:
        logger.warning(f"[EDGE TTS] Synthesis error: {exc}")
    return b""


SNAPSERVE_ANUSHA_VOICE_ID = "ca3007f96ae7499ab87d27ea3599956a"


def resolve_snapserve_voice_id(voice_id: str | None = None) -> str:
    """
    Resolves human-friendly voice names and aliases (e.g. 'Anusha', 'anusha', 'default')
    to the exact voice identifier supported by SnapServe: ca3007f96ae7499ab87d27ea3599956a.
    """
    default_id = (
        getattr(settings, "snapserve_voice_id", None)
        or os.getenv("SNAPSERVE_VOICE_ID")
        or SNAPSERVE_ANUSHA_VOICE_ID
    ).strip()
    if not voice_id:
        return default_id
    v_clean = str(voice_id).strip()
    if v_clean.lower() in ("anusha", "default", "female", "auto", "snapserve-anusha", "snapserve_anusha"):
        return default_id
    return v_clean


def reset_snapserve_cooldown() -> None:
    """Resets the SnapServe API cooldown timer for tests and retry flows."""
    global _SNAPSERVE_DISABLED_UNTIL
    _SNAPSERVE_DISABLED_UNTIL = 0.0


async def synthesize_snapserve_speech(
    text: str,
    voice_id: str | None = None,
    format: str = "wav",
    rate: float = 0.0,
    pitch: float = 0.0,
    sample_rate: int | None = 8000,
    api_key: Optional[str] = None,
) -> bytes:
    """
    Synthesizes speech using SnapServe AI TTS provider.
    Enforces voice_id ca3007f96ae7499ab87d27ea3599956a (Anusha) by default.
    Bypasses Murf completely.
    """
    text = clean_markdown_for_speech(text)
    if not text:
        return b""

    import os
    effective_api_key = (api_key or get_effective_snapserve_key() or "").strip()
    effective_voice = resolve_snapserve_voice_id(voice_id)
    api_url = (
        getattr(settings, "snapserve_api_url", None)
        or os.getenv("SNAPSERVE_API_URL")
        or "https://api.snapserve.ai/v1/tts"
    ).strip()

    logger.info(
        f"[TTS PROVIDER] provider=snapserve | voice_id={effective_voice} | format={format} | sample_rate={sample_rate}"
    )

    cache_key = f"snapserve:{effective_voice}:{format}:{sample_rate}:{rate}:{pitch}:{text}"
    if cache_key in _TTS_BYTES_CACHE:
        logger.debug(f"[SNAPSERVE TTS Cache Hit] Returning in-memory cached audio for: {text[:40]}")
        return _TTS_BYTES_CACHE[cache_key]

    audio_bytes = b""
    import time
    global _SNAPSERVE_DISABLED_UNTIL
    if effective_api_key and time.time() > _SNAPSERVE_DISABLED_UNTIL:
        try:
            from app.services.http_client import get_shared_client
            client = get_shared_client()
            headers = {
                "Authorization": f"Bearer {effective_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "text": text,
                "voice_id": effective_voice,
                "format": format.lower(),
            }
            if sample_rate:
                payload["sample_rate"] = sample_rate

            resp = await client.post(api_url, headers=headers, json=payload, timeout=8.0)
            if resp.status_code == 200:
                if resp.headers.get("content-type", "").startswith("application/json"):
                    data = resp.json()
                    raw_audio = data.get("audio") or data.get("encodedAudio") or data.get("audio_content")
                    if raw_audio and isinstance(raw_audio, str):
                        import base64
                        audio_bytes = base64.b64decode(raw_audio)
                    elif "url" in data or "audio_url" in data:
                        audio_url = data.get("url") or data.get("audio_url")
                        aud_resp = await client.get(audio_url, timeout=10.0)
                        if aud_resp.status_code == 200:
                            audio_bytes = aud_resp.content
                else:
                    audio_bytes = resp.content
            else:
                if resp.status_code in (404, 401, 403):
                    _SNAPSERVE_DISABLED_UNTIL = time.time() + 600.0  # disable for 10 minutes
                logger.warning(
                    f"[SNAPSERVE_TTS] SnapServe API returned status {resp.status_code}: {resp.text[:120]}. Falling back to high-fidelity Neural Edge TTS."
                )
        except Exception as exc:
            _SNAPSERVE_DISABLED_UNTIL = time.time() + 300.0  # disable for 5 minutes
            logger.warning(
                f"[SNAPSERVE_TTS] SnapServe network error ({type(exc).__name__}): {exc}. Falling back to high-fidelity Neural Edge TTS."
            )

    if not audio_bytes:
        # Fallback strictly to Neural Edge TTS (Murf is NEVER called for SnapServe)
        try:
            logger.info(f"[TTS_FALLBACK_START] provider=Neural Edge TTS text=\"{text[:50]}\"")
            is_tamil = any("\u0b80" <= c <= "\u0bff" for c in text)
            audio_bytes = await synthesize_edge_speech(
                text,
                voice_id="ta-IN-PallaviNeural" if is_tamil else "en-US-AriaNeural",
                rate=rate,
                pitch=pitch,
                lang="ta" if is_tamil else "en",
            )
            if audio_bytes:
                logger.info(f"[TTS_FALLBACK_SUCCESS] provider=Neural Edge TTS bytes={len(audio_bytes)}")
            else:
                logger.error(f"[TTS_FALLBACK_ERROR] provider=Neural Edge TTS returned empty audio")
        except Exception as e:
            logger.error(f"[SNAPSERVE_TTS] Edge fallback error: {e}")

    if audio_bytes:
        if len(_TTS_BYTES_CACHE) > 500:
            _TTS_BYTES_CACHE.clear()
        _TTS_BYTES_CACHE[cache_key] = audio_bytes

    return audio_bytes


def get_effective_murf_key() -> str:
    import os
    key = (getattr(settings, "murf_api_key", None) or os.getenv("MURF_API_KEY") or "").strip()
    if not key:
        try:
            from dotenv import dotenv_values
            from app.core.config import ENV_FILE
            env_vals = dotenv_values(ENV_FILE)
            key = (env_vals.get("MURF_API_KEY") or "").strip()
            if key:
                settings.murf_api_key = key
                os.environ["MURF_API_KEY"] = key
        except Exception:
            pass
    return key


async def synthesize_murf_speech(
    text: str,
    voice_id: str | None = None,
    model: str | None = None,
    rate: float = 0.0,
    pitch: float = 0.0,
    audio_format: str = "WAV",
    sample_rate: int | None = None,
    api_key: str | None = None,
    style: str | None = None,
    channel_type: str | None = None,
    volume_offset: float | None = None,
) -> bytes:
    """
    Synthesizes speech using Murf AI API with campaign-configured or default voice.
    """
    text = clean_markdown_for_speech(text)
    if not text:
        return b""

    import re
    cleaned_input = re.sub(r'<break\s+time=["\']?(\d*\.?\d+)s?["\']?\s*\/?>', " ... ", text, flags=re.IGNORECASE)
    cleaned_input = re.sub(r'\[pause:?\s*(\d*\.?\d+)s?\]', " ... ", cleaned_input, flags=re.IGNORECASE)
    cleaned_input = re.sub(r'<emotion\s+type=["\']?([a-zA-Z]+)["\']?\s*/?>', " ", cleaned_input, flags=re.IGNORECASE).strip()

    effective_api_key = (api_key or get_effective_murf_key() or "").strip()
    if not effective_api_key:
        logger.warning("[MURF TTS] No Murf API key configured.")
        return b""

    # If api_key was not explicitly passed, respect global provider setting
    if not api_key and not is_murf_available():
        logger.info("[MURF TTS] Bypassed because Murf is disabled or tts_provider is set to snapserve.")
        return b""

    raw_voice = (voice_id or settings.murf_voice_id or "Pooja").strip()
    raw_model = (model or settings.murf_model or "Falcon").strip()
    raw_style = (style or getattr(settings, "murf_style", "Conversational") or "Conversational").strip()

    voice_lower = raw_voice.lower()
    if voice_lower in ("default", "auto", ""):
        effective_voice = "Pooja"
    else:
        effective_voice = raw_voice

    model_lower = raw_model.lower()
    is_falcon = "falcon" in model_lower or model_lower in ("", "default")
    effective_model = "falcon" if is_falcon else raw_model

    cache_key = f"murf:{effective_voice}:{effective_model}:{raw_style}:{audio_format}:{sample_rate}:{rate}:{pitch}:{channel_type}:{volume_offset}:{cleaned_input}"
    if cache_key in _TTS_BYTES_CACHE:
        logger.debug(f"[MURF TTS Cache Hit] Returning in-memory cached audio for: {cleaned_input[:40]}")
        return _TTS_BYTES_CACHE[cache_key]

    logger.info(
        f"[TTS PROVIDER] provider=murf | voiceId={effective_voice} | model={effective_model} | style={raw_style} | format={audio_format} | sample_rate={sample_rate or 'default'}"
    )

    from app.services.http_client import get_shared_client
    client = get_shared_client()

    url = settings.murf_api_url or "https://global.api.murf.ai/v1/speech/stream"

    # Fast Path: Murf Falcon Streaming endpoint (<100ms ultra low latency)
    if "stream" in url or is_falcon:
        stream_url = "https://global.api.murf.ai/v1/speech/stream"
        stream_headers = {
            "api-key": effective_api_key,
            "Accept": "*/*",
            "Content-Type": "application/json",
        }
        stream_payload = {
            "text": cleaned_input,
            "voiceId": effective_voice,
            "style": raw_style,
            "model": "falcon",
            "format": audio_format or "WAV",
        }
        if sample_rate:
            stream_payload["sampleRate"] = sample_rate

        try:
            resp = await client.post(stream_url, headers=stream_headers, json=stream_payload, timeout=20.0)
            if resp.status_code == 200 and len(resp.content) > 100:
                audio_bytes = resp.content
                if len(_TTS_BYTES_CACHE) > 500:
                    _TTS_BYTES_CACHE.clear()
                _TTS_BYTES_CACHE[cache_key] = audio_bytes
                return audio_bytes
            else:
                logger.warning(f"[MURF STREAM TTS] Status {resp.status_code}: {resp.text[:200]}")
        except Exception as st_err:
            logger.warning(f"[MURF STREAM TTS] Error: {st_err}")

    headers = {
        "api-key": effective_api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    payload = {
        "voiceId": effective_voice,
        "text": cleaned_input,
        "modelVersion": "GEN2",
        "format": audio_format,
        "encodeAsBase64": True,
    }
    if channel_type:
        payload["channelType"] = channel_type
    if volume_offset is not None and volume_offset != 0.0:
        payload["volumeOffset"] = float(volume_offset)
    if sample_rate:
        payload["sampleRate"] = sample_rate
    if rate and rate != 0.0:
        payload["rate"] = int(round(rate * 100)) if abs(rate) <= 1.0 else int(rate)
    if pitch and pitch != 0.0:
        payload["pitch"] = int(round(pitch * 100)) if abs(pitch) <= 1.0 else int(pitch)

    async def _attempt(pl: dict) -> bytes:
        gen_url = "https://api.murf.ai/v1/speech/generate"
        resp = await client.post(gen_url, headers=headers, json=pl, timeout=25.0)
        if resp.status_code == 200:
            try:
                data = resp.json()
            except Exception:
                if len(resp.content) > 100:
                    return resp.content
                return b""

            # Fast path: decode base64 encodedAudio directly
            raw_audio = data.get("encodedAudio") or data.get("audio")
            if raw_audio and isinstance(raw_audio, str) and len(raw_audio) > 10:
                import base64
                try:
                    return base64.b64decode(raw_audio)
                except Exception as b64_err:
                    logger.warning(f"[MURF TTS] b64decode error: {b64_err}")

            audio_url = (
                data.get("audioFile")
                or data.get("audio_file")
                or data.get("url")
                or data.get("audio_url")
            )
            if audio_url and isinstance(audio_url, str) and audio_url.startswith("http"):
                audio_resp = await client.get(audio_url, timeout=20.0)
                if audio_resp.status_code == 200:
                    return audio_resp.content

            if len(resp.content) > 100:
                return resp.content
        elif resp.status_code in (400, 403):
            raise ValueError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        else:
            logger.warning(f"[MURF TTS] API returned status {resp.status_code}: {resp.text[:300]}")
        return b""

    res_bytes = b""
    try:
        res_bytes = await _attempt(payload)
    except ValueError as ve:
        err_str = str(ve)
        logger.warning(f"[MURF TTS] Initial attempt failed ({err_str}), trying fallback parameters...")
        # Fallback 1: GEN2 with fallback voice preserving requested audio_format
        fb_voice = effective_voice or "Pooja"
        payload_retry = {
            "voiceId": fb_voice,
            "text": cleaned_input,
            "model": "falcon",
            "style": getattr(settings, "murf_style", "Conversational") or "Conversational",
            "format": audio_format,
        }
        if sample_rate:
            payload_retry["sampleRate"] = sample_rate
        try:
            res_bytes = await _attempt(payload_retry)
        except Exception as retry_exc:
            logger.error(f"[MURF TTS] Retry with Pooja Falcon failed: {retry_exc}")
            payload_retry2 = {"voiceId": fb_voice, "text": cleaned_input, "format": audio_format}
            if sample_rate:
                payload_retry2["sampleRate"] = sample_rate
            try:
                res_bytes = await _attempt(payload_retry2)
            except Exception as retry_exc2:
                logger.error(f"[MURF TTS] Retry without model failed: {retry_exc2}")
    except Exception as exc:
        logger.error(f"[MURF TTS] Exception during synthesis: {exc}")

    if res_bytes:
        if len(_TTS_BYTES_CACHE) > 500:
            _TTS_BYTES_CACHE.clear()
        _TTS_BYTES_CACHE[cache_key] = res_bytes

    return res_bytes


async def synthesize_fish_audio_speech(
    text: str,
    api_key: Optional[str] = None,
    reference_id: Optional[str] = None,
    model: str = "speech-1.5",
    format: str = "wav",
    temperature: float = 0.7,
    top_p: float = 0.7,
    latency: str = "balanced",
    speed: float = 1.0,
    volume: float = 0.0,
) -> bytes:
    """
    Synthesizes speech using Fish Audio REST API.
    Endpoint: https://api.fish.audio/v1/tts
    """
    text = clean_markdown_for_speech(text)
    if not text:
        return b""

    effective_key = (api_key or os.getenv("FISH_AUDIO_API_KEY") or getattr(settings, "fish_audio_api_key", None) or "").strip()
    if not effective_key:
        logger.warning("[FISH AUDIO TTS] No API key configured.")
        return b""

    effective_ref = (reference_id or os.getenv("FISH_AUDIO_REFERENCE_ID") or "").strip()
    cache_key = f"fish:{effective_ref}:{model}:{format}:{temperature}:{top_p}:{latency}:{speed}:{volume}:{text}"
    if cache_key in _TTS_BYTES_CACHE:
        return _TTS_BYTES_CACHE[cache_key]

    logger.info(
        f"[TTS PROVIDER] provider=fish_audio | reference_id={effective_ref or 'default'} | model={model} | format={format} | latency={latency}"
    )

    from app.services.http_client import get_shared_client
    client = get_shared_client()

    url = "https://api.fish.audio/v1/tts"
    headers = {
        "Authorization": f"Bearer {effective_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "text": text,
        "format": format.lower(),
        "temperature": max(0.1, min(1.0, float(temperature))),
        "top_p": max(0.1, min(1.0, float(top_p))),
        "latency": "normal" if latency == "normal" else "balanced",
    }
    if effective_ref:
        payload["reference_id"] = effective_ref

    prosody: dict[str, float] = {}
    if speed is not None and speed != 1.0:
        prosody["speed"] = max(0.5, min(2.0, float(speed)))
    if volume is not None and volume != 0.0:
        prosody["volume"] = max(-10.0, min(10.0, float(volume)))
    if prosody:
        payload["prosody"] = prosody

    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=25.0)
        if resp.status_code == 200 and len(resp.content) > 100:
            audio_bytes = resp.content
            if len(_TTS_BYTES_CACHE) > 500:
                _TTS_BYTES_CACHE.clear()
            _TTS_BYTES_CACHE[cache_key] = audio_bytes
            return audio_bytes
        else:
            logger.warning(f"[FISH AUDIO TTS] API returned status {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"[FISH AUDIO TTS] Synthesis error: {exc}")
    return b""


async def resolve_tts_credential(
    provider: str,
    voice_config: Optional[dict[str, Any]] = None,
    business_id: Optional[str] = None,
    db: Optional[Any] = None,
) -> str:
    """
    Safely resolves the API credential for a specific provider.
    Order of precedence:
    1. Explicit custom key from voice_config (decrypted if encrypted)
    2. Selected saved organization integration from voice_config.credential_id
    3. Active organization integration from DB for this provider & business_id
    4. Environment variable / settings fallback
    Never exposes or logs plaintext secrets.
    """
    from app.services.email_service import decrypt_api_key

    # 1. Custom key in voice_config
    if voice_config and voice_config.get("api_key"):
        raw_key = str(voice_config["api_key"]).strip()
        if raw_key and not raw_key.startswith("••"):
            decrypted = decrypt_api_key(raw_key)
            if decrypted:
                return decrypted

    # 2. Saved organization credential reference by ID
    cred_id = voice_config.get("credential_id") if voice_config else None
    if cred_id:
        try:
            from app.db.models import Integration
            from sqlalchemy import select
            from app.db.database import AsyncSessionLocal
            
            integ = None
            if db:
                integ = await db.get(Integration, cred_id)
            else:
                async with AsyncSessionLocal() as session:
                    integ = await session.get(Integration, cred_id)
            if integ and integ.config and integ.config.get("api_key"):
                decrypted = decrypt_api_key(str(integ.config["api_key"]).strip())
                if decrypted:
                    return decrypted
        except Exception as e:
            logger.debug(f"[TTS CREDENTIAL] Error querying integration by id: {e}")

    # 3. Active organization integration for this provider
    if business_id:
        try:
            from app.db.models import Integration
            from sqlalchemy import select
            from app.db.database import AsyncSessionLocal

            integ = None
            if db:
                integ = await db.scalar(
                    select(Integration).where(
                        Integration.business_id == business_id,
                        Integration.provider == provider.lower().strip(),
                        Integration.is_active == True,
                    )
                )
            else:
                async with AsyncSessionLocal() as session:
                    integ = await session.scalar(
                        select(Integration).where(
                            Integration.business_id == business_id,
                            Integration.provider == provider.lower().strip(),
                            Integration.is_active == True,
                        )
                    )
            if integ and integ.config and integ.config.get("api_key"):
                decrypted = decrypt_api_key(str(integ.config["api_key"]).strip())
                if decrypted:
                    return decrypted
        except Exception as e:
            logger.debug(f"[TTS CREDENTIAL] Error querying integration by provider: {e}")

    # 4. Environment / Settings Fallback
    prov_clean = provider.lower().strip()
    if prov_clean == "murf":
        return get_effective_murf_key()
    elif prov_clean == "snapserve":
        return get_effective_snapserve_key()
    elif prov_clean == "elevenlabs":
        return (getattr(settings, "elevenlabs_api_key", None) or os.getenv("ELEVENLABS_API_KEY") or "").strip()
    elif prov_clean == "fish_audio":
        return (getattr(settings, "fish_audio_api_key", None) or os.getenv("FISH_AUDIO_API_KEY") or "").strip()

    return ""


async def synthesize_from_voice_config(
    text: str,
    voice_config: Optional[dict[str, Any]] = None,
    agent: Any = None,
    business_id: Optional[str] = None,
    db: Optional[Any] = None,
    requested_format: str = "wav",
    sample_rate: Optional[int] = 8000,
    for_preview: bool = False,
) -> bytes:
    """
    Authoritative synthesizer that honors campaign-level voice configuration.
    Guarantees strict isolation between concurrent campaigns and calls.
    Falls back gracefully to agent configuration and .env defaults if provider fails.
    In preview mode (for_preview=True), primary provider errors are surfaced directly.
    """
    clean_text = clean_markdown_for_speech(text)
    if not clean_text:
        return b""

    # Determine primary provider
    provider = None
    if voice_config and voice_config.get("provider"):
        provider = str(voice_config["provider"]).lower().strip()
    elif agent and getattr(agent, "voice_provider", None):
        provider = str(agent.voice_provider).lower().strip()
    else:
        provider = (getattr(settings, "tts_provider", None) or "murf").lower().strip()

    # If provider is explicitly edge_tts, check if enabled
    if provider in ("edge_tts", "edge"):
        edge_enabled = await is_edge_tts_enabled(db=db, business_id=business_id)
        if not edge_enabled:
            err_msg = "Edge TTS is currently disabled. Please enable it in Voice Configuration to use Edge TTS."
            logger.warning(f"[TTS CONFIG] {err_msg}")
            if for_preview:
                raise ValueError(err_msg)
            return b""
        v_id = (voice_config.get("voice_id") if voice_config else None) or "en-US-AriaNeural"
        return await synthesize_edge_speech(clean_text, voice_id=v_id)

    # Resolve provider-specific settings from voice_config
    p_cfg: dict[str, Any] = {}
    if voice_config:
        if isinstance(voice_config.get(provider), dict):
            p_cfg.update(voice_config[provider])
        for k, v in voice_config.items():
            if k not in ("murf", "elevenlabs", "fish_audio", "snapserve") and v is not None:
                p_cfg.setdefault(k, v)

    # Resolve credential with organization isolation
    api_key = await resolve_tts_credential(provider, voice_config=voice_config, business_id=business_id, db=db)

    audio_bytes = b""
    provider_error: Optional[str] = None

    # 1. Dispatch to selected provider
    if provider == "murf":
        v_id = p_cfg.get("voice_id") or getattr(agent, "voice_id", None) or settings.murf_voice_id or "Pooja"
        m_id = p_cfg.get("model") or getattr(agent, "voice_model_id", None) or settings.murf_model or "Falcon"
        s_id = p_cfg.get("style") or getattr(settings, "murf_style", "Conversational") or "Conversational"
        r_val = float(p_cfg.get("rate") or 0.0)
        p_val = float(p_cfg.get("pitch") or 0.0)
        fmt = (p_cfg.get("format") or requested_format or "WAV").upper()
        s_rate = int(p_cfg.get("sample_rate") or sample_rate or 8000)
        ch_type = p_cfg.get("channel_type")
        v_offset = float(p_cfg.get("volume_offset") or 0.0) if p_cfg.get("volume_offset") is not None else None

        try:
            audio_bytes = await synthesize_murf_speech(
                clean_text,
                voice_id=v_id,
                model=m_id,
                style=s_id,
                rate=r_val,
                pitch=p_val,
                audio_format=fmt,
                sample_rate=s_rate,
                api_key=api_key or None,
                channel_type=ch_type,
                volume_offset=v_offset,
            )
        except Exception as exc:
            provider_error = str(exc)
            logger.warning(f"[TTS CONFIG] Murf synthesis failed: {exc}")

    elif provider == "elevenlabs":
        v_id = p_cfg.get("voice_id") or getattr(agent, "voice_id", None) or DEFAULT_PREMADE_VOICE
        m_id = p_cfg.get("model_id") or getattr(agent, "voice_model_id", None) or "eleven_turbo_v2_5"
        stab = float(p_cfg.get("stability") if p_cfg.get("stability") is not None else getattr(agent, "voice_stability", 0.5) or 0.5)
        sim = float(p_cfg.get("similarity_boost") if p_cfg.get("similarity_boost") is not None else getattr(agent, "voice_similarity_boost", 0.75) or 0.75)
        sty = float(p_cfg.get("style") or 0.0)
        spd = float(p_cfg.get("speed") or 1.0)
        out_fmt = p_cfg.get("output_format") or ("pcm_8000" if sample_rate == 8000 and requested_format in ("pcm", "ulaw") else "mp3_44100_128")
        spk_boost = bool(p_cfg.get("use_speaker_boost", True))

        try:
            audio_bytes = await synthesize_speech(
                clean_text,
                voice_id=v_id,
                stability=stab,
                similarity_boost=sim,
                style=sty,
                model_id=m_id,
                output_format=out_fmt,
                api_key=api_key or None,
                speed=spd,
                use_speaker_boost=spk_boost,
            )
        except Exception as exc:
            provider_error = str(exc)
            logger.warning(f"[TTS CONFIG] ElevenLabs synthesis failed: {exc}")

    elif provider == "fish_audio":
        ref_id = p_cfg.get("reference_id") or p_cfg.get("voice_id") or getattr(agent, "voice_id", None) or ""
        m_id = p_cfg.get("model") or "speech-1.5"
        fmt = (p_cfg.get("format") or requested_format or "wav").lower()
        temp = float(p_cfg.get("temperature") or 0.7)
        tp = float(p_cfg.get("top_p") or 0.7)
        lat = str(p_cfg.get("latency") or "balanced")
        spd = float(p_cfg.get("speed") or 1.0)
        vol = float(p_cfg.get("volume") or 0.0)

        try:
            audio_bytes = await synthesize_fish_audio_speech(
                clean_text,
                api_key=api_key or None,
                reference_id=ref_id,
                model=m_id,
                format=fmt,
                temperature=temp,
                top_p=tp,
                latency=lat,
                speed=spd,
                volume=vol,
            )
        except Exception as exc:
            provider_error = str(exc)
            logger.warning(f"[TTS CONFIG] Fish Audio synthesis failed: {exc}")

    elif provider == "snapserve":
        v_id = resolve_snapserve_voice_id(p_cfg.get("voice_id") or getattr(agent, "voice_id", None))
        fmt = (p_cfg.get("format") or requested_format or "wav").lower()
        s_rate = int(p_cfg.get("sample_rate") or sample_rate or 8000)

        try:
            audio_bytes = await synthesize_snapserve_speech(
                clean_text,
                voice_id=v_id,
                format=fmt,
                sample_rate=s_rate,
                api_key=api_key or None,
            )
        except Exception as exc:
            provider_error = str(exc)
            logger.warning(f"[TTS CONFIG] SnapServe synthesis failed: {exc}")

    # Fallback to Neural Edge TTS if primary returned empty
    if not audio_bytes:
        edge_enabled = True
        try:
            edge_enabled = await is_edge_tts_enabled(db=db, business_id=business_id)
        except Exception:
            edge_enabled = True

        if edge_enabled:
            logger.info(f"[TTS CONFIG] Provider {provider} returned empty or failed ({provider_error}); falling back to Neural Edge TTS.")
            is_tamil = any("\u0b80" <= c <= "\u0bff" for c in clean_text)
            fallback_voice = "en-US-JennyNeural"
            v_check = (str(p_cfg.get("voice_id") or "") + " " + str(p_cfg.get("voice_name") or "")).lower()
            if is_tamil:
                fallback_voice = "ta-IN-PallaviNeural"
            elif any(m in v_check for m in ("adam", "arnold", "antoni", "joshua", "marcus", "matthew", "ken", "clint", "male")):
                fallback_voice = "en-US-GuyNeural"
            elif any(f in v_check for f in ("rachel", "bella", "domi", "pooja", "natalie", "alicia", "female")):
                fallback_voice = "en-US-JennyNeural"

            try:
                audio_bytes = await synthesize_edge_speech(
                    clean_text,
                    voice_id=fallback_voice,
                    lang="ta" if is_tamil else "en",
                )
            except Exception as exc:
                logger.error(f"[TTS CONFIG] Edge fallback failed: {exc}")

    if not audio_bytes or len(audio_bytes) < 50:
        if provider_error:
            raise ValueError(provider_error)
        raise ValueError(
            f"{provider.capitalize()} returned empty audio. Please check your API key and voice configuration."
        )

    return audio_bytes



async def synthesize_sarvam_speech(
    text: str,
    target_language_code: str = "en-IN",
    speaker: str = "kavya",
    pitch: float = 0.0,
    pace: float = 1.0,
) -> bytes:
    """Sarvam AI Text-to-Speech integration for Indian English, Tamil and regional languages."""
    api_key = (getattr(settings, "sarvam_api_key", None) or "").strip()
    if not api_key:
        return b""

    text = clean_markdown_for_speech(text)
    if not text:
        return b""

    # Map old / unrecognized speaker names to valid bulbul:v3 speakers
    valid_speakers = {
        "kavya", "aditya", "priya", "rahul", "ritu", "neha", "pooja",
        "rohan", "simran", "amit", "dev", "ishita", "shreya", "ratan"
    }
    speaker_clean = (speaker or "kavya").lower()
    if speaker_clean not in valid_speakers:
        speaker_clean = "kavya"

    # Auto-detect Tamil vs English
    if any("\u0b80" <= c <= "\u0bff" for c in text):
        target_language_code = "ta-IN"
    elif not target_language_code:
        target_language_code = "en-IN"

    cache_key = f"{text}|{target_language_code}|{speaker_clean}|{pitch}|{pace}"
    if cache_key in _TTS_BYTES_CACHE:
        return _TTS_BYTES_CACHE[cache_key]

    import base64
    from app.services.http_client import get_shared_client
    client = get_shared_client()

    url = "https://api.sarvam.ai/text-to-speech"
    headers = {
        "api-subscription-key": api_key,
        "Content-Type": "application/json",
    }
    import re
    cleaned_input = re.sub(r'<break\s+time=["\']?(\d*\.?\d+)s?["\']?\s*\/?>', " ... ", text, flags=re.IGNORECASE)
    cleaned_input = re.sub(r'\[pause:?\s*(\d*\.?\d+)s?\]', " ... ", cleaned_input, flags=re.IGNORECASE)
    cleaned_input = re.sub(r'<emotion\s+type=["\']?([a-zA-Z]+)["\']?\s*/?>', " ", cleaned_input, flags=re.IGNORECASE)
    cleaned_input = cleaned_input.strip()

    if not cleaned_input:
        return b""

    # Sarvam has a 500-character max limit per API request.
    # If text is longer, split on sentence boundaries and stitch clean PCM chunks.
    if len(cleaned_input) > 400:
        sentences = re.split(r'(?<=[.?!])\s+|\n+', cleaned_input)
        chunks: list[str] = []
        current = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(current) + len(s) + 1 <= 400:
                current = f"{current} {s}" if current else s
            else:
                if current:
                    chunks.append(current)
                if len(s) <= 400:
                    current = s
                else:
                    words = s.split()
                    w_curr = ""
                    for w in words:
                        if len(w_curr) + len(w) + 1 <= 400:
                            w_curr = f"{w_curr} {w}" if w_curr else w
                        else:
                            if w_curr:
                                chunks.append(w_curr)
                            w_curr = w
                    current = w_curr
        if current:
            chunks.append(current)

        pcm_pieces: list[bytes] = []
        for sub_chunk in chunks:
            sub_wav = await synthesize_sarvam_speech(
                text=sub_chunk,
                target_language_code=target_language_code,
                speaker=speaker_clean,
                pitch=pitch,
                pace=pace,
            )
            if len(sub_wav) > 44 and sub_wav.startswith(b"RIFF"):
                pcm_pieces.append(sub_wav[44:])
            elif sub_wav:
                pcm_pieces.append(sub_wav)

        if not pcm_pieces:
            return b""

        import io
        import wave
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            wf.writeframes(b"".join(pcm_pieces))
        full_wav = buf.getvalue()
        if len(_TTS_BYTES_CACHE) > 500:
            _TTS_BYTES_CACHE.clear()
        _TTS_BYTES_CACHE[cache_key] = full_wav
        return full_wav

    payload = {
        "inputs": [cleaned_input],
        "target_language_code": target_language_code,
        "speaker": speaker_clean,
        "pitch": pitch,
        "pace": pace,
        "loudness": 1.5,
        "speech_sample_rate": 22050,
        "enable_preprocessing": True,
        "model": "bulbul:v3",
    }

    try:
        resp = await client.post(url, headers=headers, json=payload, timeout=25.0)
        if resp.status_code == 200:
            data = resp.json()
            audios = data.get("audios", [])
            if audios:
                audio_bytes = base64.b64decode(audios[0])
                if len(_TTS_BYTES_CACHE) > 500:
                    _TTS_BYTES_CACHE.clear()
                _TTS_BYTES_CACHE[cache_key] = audio_bytes
                return audio_bytes
        else:
            logger.warning(f"Sarvam AI TTS returned status {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"Sarvam AI TTS error: {exc}")
    return b""


async def synthesize_script_audio(
    script: str,
    agent=None,
    lang: str | None = None,
    lead_name: str = "Customer",
    agent_name: str = "Alex",
    voice_id_override: str | None = None,
) -> tuple[bytes, str, list[dict]]:
    """
    Parses a campaign script into emotion + pause segments, generates speech for
    each segment with emotion-aware provider parameters, inserts exact silence
    durations for pauses, and stitches them into a combined audio file.

    Returns (audio_bytes, audio_url, segments).
    """
    from app.services.script_parser import parse_script_segments
    segments = parse_script_segments(script, strict=False, lead_name=lead_name, agent_name=agent_name)
    if not segments:
        return b"", "", []

    active_provider = (settings.tts_provider or "murf").lower().strip()
    if active_provider == "murf":
        raw_v_id = voice_id_override or getattr(agent, "voice_id", None) or settings.murf_voice_id or "Pooja"
        voice_id = "Pooja" if (not raw_v_id or raw_v_id.lower() in ("pooja", "default", "female", "auto", "en-us-natalie", "natalie", "anusha", "ta-in-iniya")) else raw_v_id
    else:
        voice_id = resolve_snapserve_voice_id(voice_id_override or getattr(agent, "voice_id", None))
    model_id = getattr(agent, "voice_model_id", None) or settings.murf_model or "Falcon"

    base_stability = getattr(agent, "voice_stability", 0.5)
    base_similarity = getattr(agent, "voice_similarity_boost", 0.75)

    audio_chunks: list[bytes] = []
    is_mp3 = bool(settings.elevenlabs_api_key)

    for seg in segments:
        seg_type = seg.get("type")
        if seg_type == "speech":
            text = seg.get("text", "").strip()
            if not text:
                continue
            emotion = (seg.get("emotion") or "neutral").lower()

            is_tamil = (lang in ("ta", "ta-IN")) or any("\u0b80" <= c <= "\u0bff" for c in text)
            chunk_bytes = b""

            if active_provider == "murf" and is_murf_available():
                chunk_bytes = await synthesize_murf_speech(
                    text,
                    voice_id=voice_id,
                    model=model_id,
                )

            if not chunk_bytes and (active_provider == "snapserve" or (active_provider != "murf" and get_effective_snapserve_key())):
                chunk_bytes = await synthesize_snapserve_speech(
                    text,
                    voice_id=resolve_snapserve_voice_id(voice_id),
                    format="wav",
                )

            if not chunk_bytes and (is_tamil or active_provider == "sarvam"):
                sarvam_settings = EMOTION_SARVAM_SETTINGS.get(emotion, {"pitch": 0.0, "pace": 1.0})
                chunk_bytes = await synthesize_sarvam_speech(
                    text,
                    target_language_code="ta-IN",
                    speaker="kavitha",
                    pitch=sarvam_settings["pitch"],
                    pace=sarvam_settings["pace"],
                )

            if not chunk_bytes and (active_provider == "elevenlabs" or settings.elevenlabs_api_key):
                emo_settings = EMOTION_ELEVENLABS_SETTINGS.get(
                    emotion,
                    {"stability": base_stability, "similarity_boost": base_similarity, "style": 0.05},
                )
                
                # Prosody enhancement for expressive tone
                speech_text = text
                if emotion == "happy" and not speech_text[-1:] in ("!", "?", "."):
                    speech_text = f"{speech_text}!"
                elif emotion == "surprised" and not speech_text[-1:] in ("!", "?"):
                    speech_text = f"{speech_text}?!"
                elif emotion == "sad" and not speech_text.endswith("..."):
                    speech_text = f"{speech_text}..."

                try:
                    chunk_bytes = await synthesize_speech(
                        speech_text,
                        voice_id=voice_id if voice_id != "ca3007f96ae7499ab87d27ea3599956a" else DEFAULT_PREMADE_VOICE,
                        stability=emo_settings["stability"],
                        similarity_boost=emo_settings["similarity_boost"],
                        style=emo_settings.get("style", 0.0),
                        model_id=model_id if model_id != "Falcon" else "eleven_turbo_v2_5",
                    )
                except Exception as exc:
                    logger.warning(f"ElevenLabs segment synthesis unavailable ({exc}); using Sarvam AI TTS fallback.")

            if not chunk_bytes and getattr(settings, "sarvam_api_key", None):
                sarvam_settings = EMOTION_SARVAM_SETTINGS.get(emotion, {"pitch": 0.0, "pace": 1.0})
                chunk_bytes = await synthesize_sarvam_speech(
                    text,
                    target_language_code="ta-IN" if is_tamil else "en-IN",
                    speaker="kavya",
                    pitch=sarvam_settings["pitch"],
                    pace=sarvam_settings["pace"],
                )

            if not chunk_bytes:
                words_count = max(1, len(text.split()))
                approx_dur = max(0.4, min(6.0, words_count * 0.35))
                chunk_bytes = generate_mp3_silence(approx_dur)

            audio_chunks.append(chunk_bytes)

        elif seg_type == "pause":
            pause_dur = float(seg.get("duration", 0.5))
            silence_bytes = generate_mp3_silence(pause_dur)
            audio_chunks.append(silence_bytes)

    if not audio_chunks:
        return b"", "", segments

    # Stitch all chunks
    combined_bytes = b"".join(audio_chunks)
    digest = hashlib.sha256(
        f"{script}|{voice_id}|{lang or 'auto'}|{lead_name}|{agent_name}|v3".encode()
    ).hexdigest()[:24]
    filename = f"script_{digest}.mp3"
    path = AUDIO_DIR / filename
    path.write_bytes(combined_bytes)

    url = f"{settings.public_base_url}/static/audio/{filename}"
    return combined_bytes, url, segments


async def synthesize_for_call(
    text: str,
    agent,
    lang: str | None = None,
    voice_id_override: str | None = None,
    is_local_test: bool = False,
    voice_config: Optional[dict[str, Any]] = None,
    business_id: Optional[str] = None,
    db: Optional[Any] = None,
) -> str | None:
    """Generates speech for one call turn using campaign voice config if present,
    or primary provider / SnapServe / Murf as fallback.
    Returns a public URL or None if TTS fails."""
    if agent is None or not text or not text.strip():
        return None

    clean_text = clean_markdown_for_speech(text)
    if not clean_text:
        return None

    # Priority 0: Campaign-level voice configuration override
    if voice_config and voice_config.get("is_active", True) is not False:
        try:
            audio_bytes = await synthesize_from_voice_config(
                clean_text,
                voice_config=voice_config,
                agent=agent,
                business_id=business_id,
                db=db,
                requested_format="wav",
                sample_rate=8000,
            )
            if audio_bytes and len(audio_bytes) > 200:
                prov = str(voice_config.get("provider", "custom")).lower()
                digest = hashlib.sha256(f"{clean_text}|{prov}|{voice_config.get('voice_id')}|{lang or 'en'}".encode()).hexdigest()[:24]
                ext = ".wav" if audio_bytes.startswith(b"RIFF") else ".mp3"
                filename = f"{digest}{ext}"
                path = AUDIO_DIR / filename
                path.write_bytes(audio_bytes)
                return f"{settings.public_base_url}/static/audio/{filename}"
        except Exception as exc:
            logger.warning(f"[TTS] Campaign voice_config synthesis failed: {exc}")

    active_provider = (settings.tts_provider or "murf").lower().strip()

    # 0. Primary Provider when configured as Murf
    if active_provider == "murf" and is_murf_available():
        raw_v_id = voice_id_override or getattr(agent, "voice_id", None) or settings.murf_voice_id or "Pooja"
        voice_id = "Pooja" if (not raw_v_id or raw_v_id.lower() in ("pooja", "default", "female", "auto", "en-us-natalie", "natalie", "anusha")) else raw_v_id
        model_id = getattr(agent, "voice_model_id", None) or settings.murf_model or "Falcon"
        try:
            audio_bytes = await synthesize_murf_speech(
                clean_text,
                voice_id=voice_id,
                model=model_id,
                audio_format="WAV",
            )
            if audio_bytes and len(audio_bytes) > 200:
                digest = hashlib.sha256(f"{clean_text}|murf|{voice_id}|{lang or 'en'}".encode()).hexdigest()[:24]
                ext = ".wav" if audio_bytes.startswith(b"RIFF") else ".mp3"
                filename = f"{digest}{ext}"
                path = AUDIO_DIR / filename
                path.write_bytes(audio_bytes)
                return f"{settings.public_base_url}/static/audio/{filename}"
        except Exception as exc:
            logger.warning(f"[TTS] Murf synthesis in synthesize_for_call failed: {exc}")

    # 1. Primary Default Provider: SnapServe AI (voice_id: ca3007f96ae7499ab87d27ea3599956a)
    if active_provider == "snapserve" or (active_provider != "murf" and (is_local_test or getattr(agent, "is_local_test", False) or get_effective_snapserve_key())):
        agent_vid = getattr(agent, "voice_id", None)
        resolved_voice_id = resolve_snapserve_voice_id(voice_id_override or agent_vid)
        audio_bytes = await synthesize_snapserve_speech(clean_text, voice_id=resolved_voice_id, format="wav")
        if audio_bytes and len(audio_bytes) > 200:
            digest = hashlib.sha256(f"{clean_text}|snapserve|{resolved_voice_id}|{lang or 'en'}".encode()).hexdigest()[:24]
            ext = ".wav" if audio_bytes.startswith(b"RIFF") else ".mp3"
            filename = f"{digest}{ext}"
            path = AUDIO_DIR / filename
            path.write_bytes(audio_bytes)
            return f"{settings.public_base_url}/static/audio/{filename}"

    # If text has emotion or break markup, route through emotion-aware segment synthesis
    if "<emotion" in text.lower() or "<break" in text.lower():
        try:
            _, url, _ = await synthesize_script_audio(
                script=text,
                agent=agent,
                lang=lang,
                voice_id_override=voice_id_override,
            )
            if url:
                return url
        except Exception as e:
            logger.warning(f"Script audio synthesis fallback in synthesize_for_call: {e}")

    voice_id = voice_id_override or settings.snapserve_voice_id or "ca3007f96ae7499ab87d27ea3599956a"
    resolved_lang = (lang or getattr(agent, "language", "en") or "en").lower()
    digest = hashlib.sha256(f"{clean_text}|{active_provider}|{voice_id}|{resolved_lang}".encode()).hexdigest()[:24]
    filename = f"{digest}.mp3"
    path = AUDIO_DIR / filename

    # Cache hit: instant sub-millisecond return
    for ext_check in [".wav", ".mp3"]:
        cached_p = AUDIO_DIR / f"{digest}{ext_check}"
        if cached_p.exists() and cached_p.stat().st_size > 1000:
            logger.debug(f"[TTS Cache Hit] Returning pre-synthesized audio: {cached_p.name}")
            return f"{settings.public_base_url}/static/audio/{cached_p.name}"

    is_tamil = (lang in ("ta", "ta-IN")) or any("\u0b80" <= c <= "\u0bff" for c in text)
    audio_bytes = b""

    SARVAM_LANG_MAP = {
        "ta": "ta-IN",
        "hi": "hi-IN",
        "te": "te-IN",
        "ml": "ml-IN",
        "kn": "kn-IN",
        "bn": "bn-IN",
        "en": "en-IN",
    }
    sarvam_lang = SARVAM_LANG_MAP.get(resolved_lang, "en-IN")

    # 2. Murf AI (only if provider explicitly configured as murf and snapserve disabled)
    murf_key = get_effective_murf_key()
    if active_provider == "murf" and murf_key:
        try:
            audio_bytes = await synthesize_murf_speech(
                clean_text,
                voice_id=voice_id,
                model=settings.murf_model or "Falcon",
                audio_format="WAV",
            )
        except Exception as exc:
            logger.warning(f"[TTS] Murf synthesis error: {exc}")

    # 3. Sarvam AI (Indian Regional Languages)
    if not audio_bytes and (active_provider == "sarvam" or (is_tamil and getattr(settings, "sarvam_api_key", None))):
        sarvam_key = (getattr(settings, "sarvam_api_key", None) or "").strip()
        if sarvam_key:
            try:
                audio_bytes = await synthesize_sarvam_speech(clean_text, target_language_code=sarvam_lang, speaker="kavitha")
            except Exception as exc:
                logger.warning(f"[TTS] Sarvam synthesis failed: {exc}")

    # 4. ElevenLabs (Optional Provider / Fallback)
    if not audio_bytes and (active_provider == "elevenlabs" or settings.elevenlabs_api_key):
        if settings.elevenlabs_api_key:
            try:
                model_id = "eleven_multilingual_v2" if resolved_lang != "en" else "eleven_turbo_v2_5"
                effective_voice = voice_id if voice_id and voice_id != "ca3007f96ae7499ab87d27ea3599956a" else DEFAULT_PREMADE_VOICE
                audio_bytes = await synthesize_speech(
                    clean_text,
                    voice_id=effective_voice,
                    stability=getattr(agent, "voice_stability", 0.5) or 0.5,
                    similarity_boost=getattr(agent, "voice_similarity_boost", 0.75) or 0.75,
                    model_id=model_id,
                )
            except Exception as exc:
                logger.warning(f"[TTS] ElevenLabs synthesis failed: {exc}")

    # 5. Neural Edge TTS Fallback
    if not audio_bytes:
        try:
            audio_bytes = await synthesize_edge_speech(
                clean_text,
                lang="ta-IN" if is_tamil else resolved_lang,
            )
        except Exception as exc:
            logger.warning(f"[TTS] Edge TTS synthesis failed: {exc}")

    if not audio_bytes or len(audio_bytes) < 500:
        logger.warning(f"[TTS] No valid audio generated for: {clean_text[:50]}")
        return None

    ext = ".wav" if audio_bytes.startswith(b"RIFF") else ".mp3"
    filename = f"{digest}{ext}"
    path = AUDIO_DIR / filename
    path.write_bytes(audio_bytes)
    return f"{settings.public_base_url}/static/audio/{filename}"


async def synthesize_preview(
    text: str,
    agent=None,
    lang: str | None = None,
    voice_config: Optional[dict[str, Any]] = None,
    business_id: Optional[str] = None,
    db: Optional[Any] = None,
) -> bytes:
    """Used by the voice preview endpoints — returns raw audio bytes."""
    # Priority 0: Agent or Campaign-level voice configuration override
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
                logger.debug(f"[TTS PREVIEW] Could not resolve agent voice_config: {e}")

        if not voice_config and getattr(agent, "voice_provider", None):
            voice_config = {
                "provider": getattr(agent, "voice_provider", None),
                "voice_id": getattr(agent, "voice_id", None),
                "voice_name": getattr(agent, "voice_name", None),
                "model_id": getattr(agent, "voice_model_id", None),
                "is_active": True,
            }

    if voice_config and voice_config.get("is_active", True) is not False:
        try:
            audio_bytes = await synthesize_from_voice_config(
                text,
                voice_config=voice_config,
                agent=agent,
                business_id=business_id or (getattr(agent, "business_id", None) if agent else None),
                db=db,
                requested_format="wav",
                sample_rate=8000,
            )
            if audio_bytes and len(audio_bytes) > 200:
                return audio_bytes
        except Exception as exc:
            logger.warning(f"[TTS PREVIEW] voice_config preview failed: {exc}")

    # Check if text has emotion or break tags
    if "<emotion" in text.lower() or "<break" in text.lower():
        raw_bytes, _, _ = await synthesize_script_audio(text, agent=agent, lang=lang)
        if raw_bytes:
            return raw_bytes

    active_provider = (getattr(settings, "tts_provider", None) or "murf").lower().strip()
    murf_key = get_effective_murf_key()
    if active_provider == "murf" and murf_key:
        raw_v_id = getattr(agent, "voice_id", None) or settings.murf_voice_id or "Pooja"
        voice_id = "Pooja" if (not raw_v_id or raw_v_id.lower() in ("default", "auto")) else raw_v_id
        audio_bytes = await synthesize_murf_speech(
            text,
            voice_id=voice_id,
            model=getattr(agent, "voice_model_id", None) or settings.murf_model or "Falcon",
        )
        if audio_bytes:
            logger.info(f"[TTS] generated_audio_bytes={len(audio_bytes)}")
            return audio_bytes

    if active_provider == "snapserve" or (active_provider != "murf" and get_effective_snapserve_key()):
        agent_vid = getattr(agent, "voice_id", None)
        voice_id = resolve_snapserve_voice_id(agent_vid)
        audio_bytes = await synthesize_snapserve_speech(
            text,
            voice_id=voice_id,
            format="wav",
        )
        if audio_bytes:
            logger.info(f"[TTS] generated_audio_bytes={len(audio_bytes)}")
            return audio_bytes

    if not settings.elevenlabs_api_key:
        return b""

    model_id = getattr(agent, "voice_model_id", None) or "eleven_turbo_v2_5"
    if lang and lang != "en" and lang != "auto":
        model_id = "eleven_multilingual_v2"

    try:
        return await synthesize_speech(
            text,
            voice_id=getattr(agent, "voice_id", None) or DEFAULT_PREMADE_VOICE,
            stability=getattr(agent, "voice_stability", 0.5),
            similarity_boost=getattr(agent, "voice_similarity_boost", 0.75),
            model_id=model_id,
        )
    except Exception as exc:
        logger.error("ElevenLabs preview failed: {}", exc)
        raise RuntimeError(str(exc)) from exc
