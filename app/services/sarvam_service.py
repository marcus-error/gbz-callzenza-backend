"""
Sarvam AI Service — Text-to-Speech (TTS), Speech-to-Text (STT), and Translation API integration.
"""
from __future__ import annotations

import os
import base64
from typing import Optional
from loguru import logger
from app.core.config import get_settings

settings = get_settings()
SARVAM_BASE_URL = "https://api.sarvam.ai"


def _get_api_key() -> str:
    key = (
        getattr(settings, "sarvam_api_key", "")
        or os.getenv("SARVAM_API_KEY", "")
        or os.getenv("SARVAM_KEY", "")
    ).strip()
    if not key or len(key) < 10 or "your_" in key.lower() or "change-me" in key.lower():
        return ""
    return key


async def synthesize_speech_sarvam(
    text: str,
    target_language_code: str = "ta-IN",
    speaker: str = "kavitha",
    pitch: float = 0.0,
    pace: float = 1.0,
    loudness: float = 1.5,
    speech_sample_rate: int = 8000,
) -> bytes:
    api_key = _get_api_key()
    if not api_key or not text or not text.strip():
        return b""

    from app.services.http_client import get_shared_client
    client = get_shared_client()

    payload = {
        "inputs": [text.strip()],
        "target_language_code": target_language_code,
        "speaker": speaker,
        "pitch": pitch,
        "pace": pace,
        "loudness": loudness,
        "speech_sample_rate": speech_sample_rate,
        "enable_preprocessing": True,
        "model": "bulbul:v3",
    }

    try:
        response = await client.post(
            f"{SARVAM_BASE_URL}/text-to-speech",
            headers={
                "api-subscription-key": api_key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
        if response.status_code == 200:
            data = response.json()
            audios = data.get("audios", [])
            if audios:
                return base64.b64decode(audios[0])
        else:
            logger.debug(f"[Sarvam TTS] Returned status {response.status_code}")
    except Exception as exc:
        logger.debug(f"[Sarvam TTS] Exception: {exc}")
    return b""


async def transcribe_speech_sarvam(
    audio_bytes: bytes,
    language_code: str = "ta-IN",
    model: str = "saaras:v2",
) -> str:
    api_key = _get_api_key()
    if not api_key or not audio_bytes:
        return ""

    from app.services.http_client import get_shared_client
    client = get_shared_client()

    files = {
        "file": ("audio.wav", audio_bytes, "audio/wav"),
    }
    data = {
        "language_code": language_code,
        "model": model,
        "with_timestamps": "false",
    }

    try:
        response = await client.post(
            f"{SARVAM_BASE_URL}/speech-to-text",
            headers={
                "api-subscription-key": api_key,
            },
            data=data,
            files=files,
            timeout=10.0,
        )
        if response.status_code == 200:
            return response.json().get("transcript", "").strip()
    except Exception as exc:
        logger.debug(f"[Sarvam STT] Exception: {exc}")
    return ""
