from __future__ import annotations

import re
from typing import Optional
from loguru import logger
from app.core.config import get_settings

settings = get_settings()


async def transcribe_audio(audio_bytes: bytes, mimetype: str = "audio/wav", language_code: str = "ta-IN") -> str:
    logger.info(f"[STT] Transcribing audio payload (size: {len(audio_bytes)} bytes, language_code: {language_code})")
    
    if settings.deepgram_api_key:
        try:
            from app.services.http_client import get_shared_client
            client = get_shared_client()
            lang_param = ""
            if language_code:
                norm_lang = language_code.lower()
                if norm_lang.startswith("en") or norm_lang.startswith("hi") or norm_lang.startswith("es"):
                    lang_param = f"&language={language_code}"
            url = f"https://api.deepgram.com/v1/listen?model=nova-2&smart_formatting=false&punctuate=true{lang_param}"
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Token {settings.deepgram_api_key}",
                    "Content-Type": mimetype,
                },
                content=audio_bytes,
                timeout=4.0,
            )
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", {})
            channels = results.get("channels", [])
            transcript = ""
            if channels and len(channels) > 0:
                alts = channels[0].get("alternatives", [])
                if alts and len(alts) > 0:
                    transcript = (alts[0].get("transcript") or "").strip()

            is_final = data.get("is_final")
            if is_final is None:
                is_final = results.get("is_final")

            speech_final = data.get("speech_final")
            if speech_final is None:
                speech_final = results.get("speech_final")

            if is_final is False and not speech_final:
                is_final_flag = False
            else:
                is_final_flag = True if (is_final or speech_final or (is_final is None and speech_final is None)) else False

            logger.info(f"[STT][DEEPGRAM_EVENT] is_final={is_final} speech_final={speech_final} transcript_len={len(transcript)}")

            if is_final_flag and transcript:
                logger.info(f"[STT][FINAL] transcript=\"{transcript}\" is_final={is_final} speech_final={speech_final}")
                logger.info(f"[STT][RESULT] transcript received: \"{transcript}\"")
                return transcript
            else:
                if not is_final_flag:
                    logger.info(f"[STT][INTERIM] partial/interim transcript ignored: \"{transcript}\"")
                else:
                    logger.info(f"[STT] final event received with empty transcript")
                return ""
        except Exception as exc:
            logger.warning(f"[STT] Deepgram transcription failed: {exc}")

    if getattr(settings, "sarvam_api_key", ""):
        try:
            from app.services.sarvam_service import transcribe_speech_sarvam
            res = await transcribe_speech_sarvam(audio_bytes, language_code=language_code)
            if res and res.strip():
                logger.info(f"[STT] final transcript (Sarvam, len={len(res)}): '{res}'")
                return res.strip()
        except Exception as exc:
            logger.warning(f"[STT] Sarvam transcription failed: {exc}")

    # Fallback to Google Gemini STT
    if getattr(settings, "gemini_api_key", ""):
        try:
            gemini_res = await transcribe_speech_gemini(audio_bytes, mimetype=mimetype)
            if gemini_res and gemini_res.strip():
                # Sanity-check: reject results with >20% non-printable characters (Gemini hallucination on bad audio)
                non_print = sum(1 for c in gemini_res if ord(c) < 32 or (0x7F <= ord(c) <= 0x9F))
                if non_print / max(1, len(gemini_res)) > 0.20:
                    logger.warning(f"[STT] Discarding Gemini result with {non_print} non-printable chars (likely corrupt audio)")
                    return ""
                logger.info(f"[STT] final transcript (Gemini, len={len(gemini_res)}): '{gemini_res}'")
                return gemini_res.strip()
            return ""
        except Exception as exc:
            logger.warning(f"[STT] Gemini transcription failed: {exc}")
            return ""

    return ""


_GENAI_CLIENT = None
_VERIFIED_GEMINI_STT_MODEL: Optional[str] = None

def get_genai_client():
    global _GENAI_CLIENT
    if _GENAI_CLIENT is None and getattr(settings, "gemini_api_key", ""):
        from google import genai
        _GENAI_CLIENT = genai.Client(api_key=settings.gemini_api_key)
    return _GENAI_CLIENT


_PHANTOM_NOISE_TOKENS = {
    "de", "mama", "um", "uh", "ah", "eh", "er", "mm", "mmm", "da", "ha",
    "00:00", "0:00", "0000", "000", "00", "0", "none", "null", "empty", "empty string", "empty string.",
    "n/a", "silence", "silence.", "[silence]", "[applause]", "[music]", "[laughter]", "[noise]"
}


async def transcribe_speech_gemini(audio_bytes: bytes, mimetype: str = "audio/wav") -> str:
    """Transcribes audio using Google Gemini multimodal audio recognition with low-latency flash-lite."""
    global _VERIFIED_GEMINI_STT_MODEL
    if not getattr(settings, "gemini_api_key", "") or not audio_bytes:
        return ""
    try:
        import asyncio
        import time
        from google.genai import types

        def _sync_transcribe() -> str:
            global _VERIFIED_GEMINI_STT_MODEL
            t0 = time.perf_counter()
            client = get_genai_client()
            if not client:
                return ""
            prompt = (
                "You are an accurate real-time speech-to-text transcriber for phone calls. "
                "Transcribe exactly what the caller speaks verbatim in English, Tamil, Hindi, or Tanglish. "
                "Do NOT hallucinate, guess, or invent words. "
                "Do NOT transcribe line static, clicks, breath, or background murmurs. "
                "If the audio contains only silence, background noise, or no clear words, reply with an empty string. "
                "Output ONLY the exact spoken transcription."
            )
            candidate_models = [
                "gemini-3.5-flash",
                "gemini-3.5-transcribe",
                "gemini-flash-latest",
                "gemini-3.5-flash-lite",
            ]
            if _VERIFIED_GEMINI_STT_MODEL and _VERIFIED_GEMINI_STT_MODEL in candidate_models:
                ordered_models = [_VERIFIED_GEMINI_STT_MODEL] + [m for m in candidate_models if m != _VERIFIED_GEMINI_STT_MODEL]
            else:
                ordered_models = candidate_models

            for m in ordered_models:
                try:
                    response = client.models.generate_content(
                        model=m,
                        contents=[
                            types.Part.from_bytes(data=audio_bytes, mime_type=mimetype),
                            prompt,
                        ],
                    )
                    text = (response.text or "").strip()
                    clean_text = re.sub(r"```[a-zA-Z]*|```", "", text).strip()
                    dur_ms = int((time.perf_counter() - t0) * 1000)
                    _VERIFIED_GEMINI_STT_MODEL = m
                    
                    normalized_lower = clean_text.lower().strip(".,!?:;\"' ")
                    if not clean_text or normalized_lower in _PHANTOM_NOISE_TOKENS or re.match(r"^0+$", normalized_lower) or re.match(r"^\[?\d{1,2}:\d{2}\]?$", clean_text):
                        logger.debug(f"[STT] Filtered out empty/phantom transcript '{clean_text}': {dur_ms} ms (model={m})")
                        return ""
                    if clean_text:
                        logger.info(f"[STT] input_audio_bytes={len(audio_bytes)} transcript='{clean_text}'")
                        logger.info(f"[STT] Final transcript received: {dur_ms} ms | text='{clean_text}' (model={m})")
                        return clean_text
                except Exception as inner_e:
                    logger.debug(f"[STT] Model {m} failed: {inner_e}")
                    continue
            return ""

        return await asyncio.wait_for(asyncio.to_thread(_sync_transcribe), timeout=4.5)
    except asyncio.TimeoutError:
        logger.warning("[STT] Gemini transcription timed out after 4.5s")
        return ""
    except Exception as exc:
        logger.warning(f"[STT] Gemini transcription error: {exc}")
        return ""

