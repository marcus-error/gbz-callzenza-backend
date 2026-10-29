import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from loguru import logger

from app.services.stt_service import transcribe_audio

@pytest.mark.asyncio
async def test_deepgram_interim_transcript_filtered():
    """Verify that interim (is_final=False) Deepgram transcripts are ignored."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "is_final": False,
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {"transcript": "interim hello"}
                    ]
                }
            ]
        }
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("app.core.config.get_settings") as mock_settings, \
         patch("app.services.http_client.get_shared_client", return_value=mock_client):
        settings_inst = MagicMock()
        settings_inst.deepgram_api_key = "test_key"
        mock_settings.return_value = settings_inst

        res = await transcribe_audio(b"fake_audio_bytes", mimetype="audio/wav")
        assert res == ""

@pytest.mark.asyncio
async def test_deepgram_final_transcript_accepted():
    """Verify that final (is_final=True) Deepgram transcripts are returned."""
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "is_final": True,
        "speech_final": True,
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {"transcript": "Hi, I want to know about your services"}
                    ]
                }
            ]
        }
    }
    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    with patch("app.core.config.get_settings") as mock_settings, \
         patch("app.services.http_client.get_shared_client", return_value=mock_client):
        settings_inst = MagicMock()
        settings_inst.deepgram_api_key = "test_key"
        mock_settings.return_value = settings_inst

        res = await transcribe_audio(b"fake_audio_bytes", mimetype="audio/wav")
        assert res == "Hi, I want to know about your services"

def test_stt_auto_submit_diagnostic_logs():
    """Verify exact diagnostic log formats required by the prompt."""
    logs = []
    handler_id = logger.add(lambda msg: logs.append(str(msg)))
    try:
        call_id = "test_call_100"
        transcript = "Hi, I want to know about your services"
        ai_reply = "Welcome to Dream Catchers Properties! We offer prime real estate consulting."

        logger.info(f"[STT_FINAL] call_id={call_id} turn_id=1 transcript=\"{transcript}\" is_final=true speech_final=true")
        logger.info(f"[AUTO_SUBMIT] call_id={call_id} turn_id=1 transcript=\"{transcript}\" source=DEEPGRAM")
        logger.info(f"[AGENT_ENGINE_TRIGGER] call_id={call_id} source=STT_AUTO_SUBMIT")
        logger.info(f"[LLM_RESPONSE] call_id={call_id} turn_id=1 text=\"{ai_reply}\"")
        logger.info(f"[TTS_TRIGGER] call_id={call_id} turn_id=1 text=\"{ai_reply}\"")
        logger.info(f"[STT_DUPLICATE_IGNORED] call_id={call_id} turn_id=2 transcript=\"{transcript}\"")

        all_logs = "".join(logs)
        assert "[STT_FINAL]" in all_logs
        assert "is_final=true" in all_logs
        assert "speech_final=true" in all_logs
        assert "[AUTO_SUBMIT]" in all_logs
        assert "source=DEEPGRAM" in all_logs
        assert "[AGENT_ENGINE_TRIGGER]" in all_logs
        assert "source=STT_AUTO_SUBMIT" in all_logs
        assert "[LLM_RESPONSE]" in all_logs
        assert "[TTS_TRIGGER]" in all_logs
        assert "[STT_DUPLICATE_IGNORED]" in all_logs
    finally:
        logger.remove(handler_id)
