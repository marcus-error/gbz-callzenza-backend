"""
Test Suite for Dynamic Voice Management, Provider Fetching, Manual Voice ID Entry, and AI Agent Voice Assignment.
"""
import io
import struct
import wave
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import Response

from app.db.database import AsyncSessionLocal
from app.db.models import AIAgent, Integration, User, Business
from app.services.email_service import encrypt_api_key, decrypt_api_key
from app.services.tts_service import synthesize_from_voice_config, synthesize_preview
from app.api.routes.voice_configs import (
    _fetch_provider_voices_internal,
    SUPPORTED_PROVIDERS,
    VoiceConfigCreate,
)
from app.services.sip_ai_bot import synthesize_text_to_mulaw


def _create_mock_wav(duration_sec: float = 0.5, sample_rate: int = 8000) -> bytes:
    """Creates a valid 16-bit mono 8000Hz PCM WAV in memory."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        num_samples = int(duration_sec * sample_rate)
        samples = [int(10000 * 0.5) for _ in range(num_samples)]
        raw = struct.pack(f"<{len(samples)}h", *samples)
        wf.writeframes(raw)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_dynamic_provider_voice_fetching_schemas():
    """Verify ElevenLabs, Fish Audio, and Murf AI each fetch with their respective endpoints and schemas."""
    # 1. ElevenLabs Mock Response
    eleven_resp = Response(
        status_code=200,
        json={
            "voices": [
                {
                    "voice_id": "eleven_v1",
                    "name": "Rachel Premium",
                    "labels": {"gender": "female", "accent": "american"},
                    "preview_url": "https://elevenlabs.io/sample1.mp3",
                }
            ]
        },
    )
    # 2. Fish Audio Mock Response
    fish_resp = Response(
        status_code=200,
        json={
            "items": [
                {
                    "_id": "fish_model_999",
                    "title": "Fish Custom Voice 1",
                    "languages": ["en", "ja"],
                }
            ]
        },
    )
    # 3. Murf AI Mock Response
    murf_resp = Response(
        status_code=200,
        json=[
            {
                "voiceId": "murf_v1_marcus",
                "displayName": "Marcus",
                "gender": "male",
                "locale": "en-US",
            }
        ],
    )

    with patch("httpx.AsyncClient.get") as mock_get:
        # ElevenLabs
        mock_get.return_value = eleven_resp
        eleven_res = await _fetch_provider_voices_internal("elevenlabs", api_key="sk_test_key")
        eleven_voices = eleven_res["voices"]
        assert len(eleven_voices) == 1
        assert eleven_voices[0]["id"] == "eleven_v1"
        assert eleven_voices[0]["voice_name"] == "Rachel Premium"
        assert eleven_voices[0]["gender"] == "female"

        # Fish Audio
        mock_get.return_value = fish_resp
        fish_res = await _fetch_provider_voices_internal("fish_audio", api_key="fish_key_123")
        fish_voices = fish_res["voices"]
        assert len(fish_voices) == 1
        assert fish_voices[0]["id"] == "fish_model_999"
        assert fish_voices[0]["voice_name"] == "Fish Custom Voice 1"

        # Murf AI
        mock_get.return_value = murf_resp
        murf_res = await _fetch_provider_voices_internal("murf", api_key="murf_key_abc")
        murf_voices = murf_res["voices"]
        assert len(murf_voices) == 1
        assert murf_voices[0]["id"] == "murf_v1_marcus"
        assert murf_voices[0]["voice_name"] == "Marcus"


@pytest.mark.asyncio
async def test_ai_agent_voice_assignment_and_persistence():
    """Verify creating an AI Agent, linking a saved Voice Configuration, and retrieving accurate configuration."""
    import uuid
    test_bid = f"biz_test_{uuid.uuid4().hex[:8]}"
    integ_id = f"integ_{uuid.uuid4().hex[:8]}"
    agent_id = f"agent_{uuid.uuid4().hex[:8]}"

    async with AsyncSessionLocal() as db:
        # 1. Create a saved voice config (Integration) for Murf
        integ = Integration(
            id=integ_id,
            business_id=test_bid,
            provider="murf",
            config={
                "name": "Murf Marcus Sales Configuration",
                "voice_id": "en-US-marcus",
                "voice_name": "Marcus",
                "model_id": "Falcon",
                "api_key": encrypt_api_key("murf_secret_123"),
            },
            is_active=True,
        )
        db.add(integ)

        # 2. Create AI Agent linking to this configuration
        agent = AIAgent(
            id=agent_id,
            business_id=test_bid,
            name="Marcus Sales Specialist",
            voice_config_id=integ.id,
            voice_provider="murf",
            voice_id="en-US-marcus",
            voice_name="Marcus",
            voice_model_id="Falcon",
        )
        db.add(agent)
        await db.commit()

        # 3. Reload and verify persistence
        saved_agent = await db.get(AIAgent, agent_id)
        assert saved_agent is not None
        assert saved_agent.voice_config_id == integ_id
        assert saved_agent.voice_provider == "murf"
        assert saved_agent.voice_id == "en-US-marcus"
        assert saved_agent.voice_name == "Marcus"


@pytest.mark.asyncio
async def test_dynamic_agent_voice_reassignment_updates_tts():
    """Verify changing an AI Agent's voice config dynamically updates subsequent TTS calls without restart."""
    import uuid
    test_bid = f"biz_test_{uuid.uuid4().hex[:8]}"
    agent_id = f"agent_reassign_{uuid.uuid4().hex[:8]}"
    mock_audio = _create_mock_wav()

    async with AsyncSessionLocal() as db:
        # Setup Agent initially with ElevenLabs
        agent = AIAgent(
            id=agent_id,
            business_id=test_bid,
            name="Adaptable Agent",
            voice_provider="elevenlabs",
            voice_id="21m00Tcm4TlvDq8ikWAM",
            voice_name="Rachel",
            voice_model_id="eleven_multilingual_v2",
        )
        db.add(agent)
        await db.commit()

    with patch("app.services.tts_service.synthesize_speech", new_callable=AsyncMock) as mock_eleven, \
         patch("app.services.tts_service.synthesize_murf_speech", new_callable=AsyncMock) as mock_murf:

        mock_eleven.return_value = mock_audio
        mock_murf.return_value = mock_audio

        # First TTS synthesis -> Uses ElevenLabs Rachel
        res1 = await synthesize_text_to_mulaw("Hello, testing first voice.", agent=agent)
        assert len(res1) > 0
        mock_eleven.assert_called_once()
        assert mock_eleven.call_args.kwargs["voice_id"] == "21m00Tcm4TlvDq8ikWAM"
        mock_murf.assert_not_called()

        # Now reassign agent's voice to Murf Pooja
        async with AsyncSessionLocal() as db:
            ag = await db.get(AIAgent, agent_id)
            ag.voice_provider = "murf"
            ag.voice_id = "Pooja"
            ag.voice_name = "Pooja"
            ag.voice_model_id = "Falcon"
            db.add(ag)
            await db.commit()
            await db.refresh(ag)
            updated_agent = ag

        # Second TTS synthesis -> Immediately uses Murf Pooja without restart
        mock_eleven.reset_mock()
        mock_murf.reset_mock()

        with patch("app.services.tts_service.resolve_tts_credential", return_value="mock_murf_key"):
            res2 = await synthesize_text_to_mulaw("Hello, testing newly assigned voice.", agent=updated_agent)
            assert len(res2) > 0
            mock_murf.assert_called_once()
            assert mock_murf.call_args.kwargs["voice_id"] == "Pooja"
            mock_eleven.assert_not_called()
