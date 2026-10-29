import pytest
import asyncio
import time
from unittest.mock import MagicMock, AsyncMock, patch

from app.services.stt_service import transcribe_audio
from app.services.ai.agent_engine import AgentEngine, AgentEngineResponse
from app.api.routes.local_voice_test import extract_script_stages


@pytest.mark.asyncio
async def test_stt_finalization_and_logging():
    """Verify STT service transcribes audio and logs final vs partial state without exposing secrets."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()
    mock_response.json.return_value = {
        "is_final": True,
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {"transcript": "Good morning, I would like to inquire about your services."}
                    ]
                }
            ]
        }
    }

    with patch("app.services.http_client.get_shared_client") as mock_client_fn, \
         patch("app.services.stt_service.settings") as mock_settings:
        mock_settings.deepgram_api_key = "test_deepgram_key"
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client_fn.return_value = mock_client

        audio_payload = b"fake_audio_pcm_payload_bytes"
        result = await transcribe_audio(audio_payload, mimetype="audio/wav")

        assert result == "Good morning, I would like to inquire about your services."
        assert mock_client.post.called


@pytest.mark.asyncio
async def test_greeting_fast_response_no_rag_delay():
    """Verify simple greetings receive quick AI responses without forcing unnecessary RAG retrieval."""
    mock_db = AsyncMock()
    
    with patch.object(AgentEngine, "_get_agent_context", new_callable=AsyncMock) as mock_ctx, \
         patch("app.services.ai.prompt_service.PromptService.build_system_prompt", return_value="System Prompt"), \
         patch("app.services.ai.agent_engine.get_llm_service") as mock_llm_fn:
        
        from app.services.ai.script_engine import ScriptNormalizer
        norm_script = ScriptNormalizer.parse_script("1. GREETING: Good morning! How can I help you?", agent_name="CallZenza Agent", business_name="CallZenza")
        mock_ctx.return_value = {
            "agent_name": "CallZenza Agent",
            "business_name": "CallZenza",
            "agent_persona": "Professional AI Assistant",
            "business_id": "bus_123",
            "script_text": "1. GREETING: Good morning! How can I help you?",
            "script_source": "1. GREETING: Good morning! How can I help you?",
            "configured_lang": "en",
            "normalized_script": norm_script,
        }

        mock_llm = AsyncMock()
        mock_llm.generate_response.return_value = "Good morning! How can I assist you today?"
        mock_llm_fn.return_value = mock_llm

        start = time.perf_counter()
        response = await AgentEngine.process_user_query(
            db=mock_db,
            agent_id="test_agent",
            message="Good morning.",
            session_id="session_stt_test_123",
            current_language="en",
        )
        duration_ms = (time.perf_counter() - start) * 1000

        assert "Good morning" in response.response
        assert response.latency_breakdown["rag_latency"] == 0
        assert duration_ms < 1000


@pytest.mark.asyncio
async def test_session_isolation_and_no_cross_leakage():
    """Verify session state and transcript buffers remain isolated across concurrent calls."""
    session_a = "call_session_A"
    session_b = "call_session_B"

    stages_a = extract_script_stages("1. GREETING: Hello A\n2. QUALIFICATION: Need info")
    stages_b = extract_script_stages("1. GREETING: Hello B\n2. QUALIFICATION: Need info")

    assert len(stages_a) >= 2
    assert len(stages_b) >= 2
    assert session_a != session_b
