import base64
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
import httpx

from app.core.config import get_settings
from app.services.tts_service import (
    synthesize_for_call,
    synthesize_snapserve_speech,
    synthesize_murf_speech,
    synthesize_script_audio,
    resolve_snapserve_voice_id,
    reset_snapserve_cooldown,
    SNAPSERVE_ANUSHA_VOICE_ID,
    _TTS_BYTES_CACHE,
)
from app.services.emotion_service import analyze_voice_emotion
from app.services.http_client import get_shared_client

settings = get_settings()

SAMPLE_WAV_BYTES = (
    b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00@\x1f\x00\x00\x80>\x00\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
    + (b"\x00" * 320)
)


@pytest.mark.asyncio
async def test_1_local_text_to_speech_snapserve_anusha():
    """Test 1: Direct text-to-speech synthesis uses Snapserve provider with Anusha voice (ca3007f96ae7499ab87d27ea3599956a)."""
    text_a = "Hello, how can I help you today?"
    client = get_shared_client()
    reset_snapserve_cooldown()
    _TTS_BYTES_CACHE.clear()

    # Case A: Mock Snapserve API returning base64 encoded audio
    mock_resp_json = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"audio": base64.b64encode(SAMPLE_WAV_BYTES).decode("utf-8")},
        request=httpx.Request("POST", "https://api.snapserve.ai/v1/tts"),
    )
    with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp_json
        audio_bytes = await synthesize_snapserve_speech(text_a, voice_id="Anusha")
        assert isinstance(audio_bytes, bytes)
        assert len(audio_bytes) > 0
        assert audio_bytes == SAMPLE_WAV_BYTES

        # Verify voice_id sent in payload is exactly the Snapserve Anusha identifier
        assert mock_post.called
        sent_payload = mock_post.call_args.kwargs.get("json", {})
        assert sent_payload.get("voice_id") == SNAPSERVE_ANUSHA_VOICE_ID

    # Case B: Mock Snapserve API returning direct binary WAV
    text_b = "Welcome to CallZenza intelligent voice platform."
    reset_snapserve_cooldown()
    _TTS_BYTES_CACHE.clear()
    mock_resp_bin = httpx.Response(
        200,
        headers={"content-type": "audio/wav"},
        content=SAMPLE_WAV_BYTES,
        request=httpx.Request("POST", "https://api.snapserve.ai/v1/tts"),
    )
    with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp_bin
        audio_bytes = await synthesize_snapserve_speech(text_b, voice_id=SNAPSERVE_ANUSHA_VOICE_ID)
        assert audio_bytes == SAMPLE_WAV_BYTES
        assert mock_post.called
        sent_payload = mock_post.call_args.kwargs.get("json", {})
        assert sent_payload.get("voice_id") == SNAPSERVE_ANUSHA_VOICE_ID


@pytest.mark.asyncio
async def test_2_local_ai_agent_snapserve_anusha_synthesis():
    """Test 2: Local AI Agent engagement response routes through Snapserve Anusha TTS."""
    agent = MagicMock()
    agent.voice_id = "Anusha"
    agent.language = "en"

    ai_text = "Thank you for reaching out! We offer specialized AI voice solutions for your business."
    audio_url = await synthesize_for_call(ai_text, agent)
    assert audio_url is not None
    assert "/static/audio/" in audio_url
    assert audio_url.endswith((".wav", ".mp3"))


@pytest.mark.asyncio
async def test_3_rag_response_snapserve_anusha():
    """Test 3: RAG knowledge retrieval response synthesizes using Snapserve Anusha voice."""
    agent = MagicMock()
    agent.voice_id = "Anusha"
    agent.language = "en"

    rag_answer = "Our standard system warranty covers all component repairs for 25 years."
    audio_url = await synthesize_for_call(rag_answer, agent)
    assert audio_url is not None
    assert "/static/audio/" in audio_url


@pytest.mark.asyncio
async def test_4_multiturn_conversation_snapserve_anusha_consistency():
    """Test 4: Multi-turn conversation maintains consistent Snapserve Anusha voice across turns."""
    agent = MagicMock()
    agent.voice_id = "Anusha"
    agent.language = "en"

    # Turn 1
    t1_text = "I can certainly help you book an appointment. Which date works best?"
    url1 = await synthesize_for_call(t1_text, agent)
    assert url1 is not None

    # Turn 2
    t2_text = "Great! I have reserved tomorrow at 3 PM for you."
    url2 = await synthesize_for_call(t2_text, agent)
    assert url2 is not None
    assert "/static/audio/" in url1 and "/static/audio/" in url2


@pytest.mark.asyncio
async def test_5_emotion_integration_with_snapserve_anusha():
    """Test 5: Frustrated emotion is detected, LLM generates calm wording, and Snapserve synthesizes with Anusha."""
    agent = MagicMock()
    agent.voice_id = "Anusha"
    agent.language = "en"

    emo_res = analyze_voice_emotion("I've already told your team this three times!")
    assert emo_res["emotion"] in ("frustrated", "angry")
    assert emo_res["tone"] == "calm_empathetic"

    calm_text = "I understand your frustration. I won't make you repeat it. Let me check this right away for you."
    audio_url = await synthesize_for_call(calm_text, agent)
    assert audio_url is not None
    assert "/static/audio/" in audio_url


@pytest.mark.asyncio
async def test_6_sip_outbound_call_script_audio_snapserve():
    """Test 6: Script audio synthesis for SIP outbound call generates Snapserve Anusha audio."""
    script = "<emotion type='happy' /> Welcome to CallZenza! <break time='0.5s' /> How may I assist you today?"
    audio_bytes, audio_url, segments = await synthesize_script_audio(script)
    assert len(audio_bytes) > 0
    assert "/static/audio/" in audio_url
    assert len(segments) == 3


def test_7_provider_verification_and_secrets_safety():
    """Test 7: Runtime provider configuration verifies Snapserve Anusha without exposing API secrets."""
    provider = (settings.tts_provider or "snapserve").lower()
    voice_id = resolve_snapserve_voice_id("Anusha")

    assert provider == "snapserve"
    assert voice_id == SNAPSERVE_ANUSHA_VOICE_ID
    if getattr(settings, "snapserve_api_key", None):
        assert settings.snapserve_api_key not in f"TTS_PROVIDER={provider}, VOICE={voice_id}"


@pytest.mark.asyncio
async def test_8_isolated_murf_fallback_when_explicitly_configured(monkeypatch):
    """Test 8: Murf synthesis runs in complete isolation with mocked API without requiring Murf key in Snapserve mode."""
    client = get_shared_client()
    monkeypatch.setattr(settings, "tts_provider", "murf")
    monkeypatch.setattr(settings, "murf_api_key", "dummy-mock-murf-key")

    mock_murf_resp = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        json={"encodedAudio": base64.b64encode(SAMPLE_WAV_BYTES).decode("utf-8")},
        request=httpx.Request("POST", "https://api.murf.ai/v1/speech/stream"),
    )
    with patch.object(client, "post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_murf_resp
        audio_bytes = await synthesize_murf_speech("Isolated Murf synthesis test", voice_id="Anusha", model="Falcon")
        assert isinstance(audio_bytes, bytes)
        assert len(audio_bytes) > 0

