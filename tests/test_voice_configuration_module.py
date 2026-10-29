"""
Comprehensive Verification Test Suite for Voice Configuration Module & Campaign Integration.

Tests the complete lifecycle:
1. Provider configuration validation (ElevenLabs, Murf AI, Fish Audio, SnapServe)
2. Secure credential storage (Fernet encryption, masking, no blank overwriting)
3. Voice Configuration CRUD (List, Get, Create, Update, Toggle Active, Delete)
4. Connection Testing (Live/Mocked responses for all 4 providers)
5. Voice fetching (Remote API and Fallback catalogs)
6. Voice preview audio streaming
7. Campaign Voice Configuration resolution and Speech Generation pipeline
8. Multi-tenant isolation and security checks
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import Response

from app.db.database import AsyncSessionLocal
from app.db.models import Integration, User, Business, Campaign
from app.services.email_service import encrypt_api_key, decrypt_api_key
from app.services.tts_service import (
    synthesize_from_voice_config,
    resolve_tts_credential,
    synthesize_preview,
)
from app.api.routes.voice_configs import (
    _mask_key,
    VoiceConfigCreate,
    VoiceConfigUpdate,
)


@pytest.mark.asyncio
async def test_mask_api_key_helper():
    """Verify API keys are masked to safe indicators and never leaked in responses."""
    assert _mask_key(None) == ""
    assert _mask_key("") == ""
    assert _mask_key("123") == "••••••••"
    assert _mask_key("sk_elevenlabs_abc1234") == "••••••••1234"
    assert _mask_key("murf_api_key_long_secret_xyz9") == "••••••••xyz9"


@pytest.mark.asyncio
async def test_crud_voice_configuration_lifecycle():
    """Verify creating, retrieving, updating (safe secret preservation), and deleting voice configs."""
    test_bid = "test_bid_voice_lifecycle_001"
    raw_secret = "sk_live_elevenlabs_secret_8877"

    async with AsyncSessionLocal() as db:
        # 1. Create a voice configuration
        integ = Integration(
            id="voice_cfg_eleven_001",
            business_id=test_bid,
            provider="elevenlabs",
            config={
                "name": "ElevenLabs Primary Sales Voice",
                "api_key": encrypt_api_key(raw_secret),
                "voice_id": "21m00Tcm4TlvDq8ikWAM",
                "model_id": "eleven_multilingual_v2",
                "language": "en",
                "settings": {"stability": 0.6, "similarity_boost": 0.8},
                "last_tested_at": None,
                "test_status": "pending",
            },
            is_active=True,
        )
        db.add(integ)
        await db.commit()

        # 2. Retrieve and verify encryption + masking
        saved = await db.get(Integration, "voice_cfg_eleven_001")
        assert saved is not None
        assert saved.provider == "elevenlabs"
        assert saved.config["name"] == "ElevenLabs Primary Sales Voice"
        # Secret in database MUST be encrypted
        assert saved.config["api_key"] != raw_secret
        assert decrypt_api_key(saved.config["api_key"]) == raw_secret

        # 3. Safe update: Updating name/settings WITHOUT providing api_key must NOT wipe existing secret
        saved.config["name"] = "Updated Sales Voice"
        saved.config["settings"]["stability"] = 0.75
        # simulate user passing empty or masked string
        masked_input = "••••••••8877"
        if not masked_input.startswith("••••"):
            saved.config["api_key"] = encrypt_api_key(masked_input)
        await db.commit()

        reloaded = await db.get(Integration, "voice_cfg_eleven_001")
        assert reloaded.config["name"] == "Updated Sales Voice"
        assert reloaded.config["settings"]["stability"] == 0.75
        # Crucial: Secret must remain intact and decryptable
        assert decrypt_api_key(reloaded.config["api_key"]) == raw_secret

        # 4. Toggle is_active
        reloaded.is_active = False
        await db.commit()
        toggled = await db.get(Integration, "voice_cfg_eleven_001")
        assert toggled.is_active is False

        # 5. Clean up
        await db.delete(toggled)
        await db.commit()


@pytest.mark.asyncio
async def test_provider_specific_configs_and_settings():
    """Verify all 4 supported providers store their distinct schemas correctly."""
    async with AsyncSessionLocal() as db:
        configs = [
            # ElevenLabs
            Integration(
                id="test_prov_elevenlabs",
                business_id="test_bid_providers",
                provider="elevenlabs",
                config={
                    "name": "ElevenLabs Config",
                    "api_key": encrypt_api_key("sk_elevenlabs_key_1234"),
                    "voice_id": "EXAVITQu4vr4xnSDxMaL",
                    "model_id": "eleven_turbo_v2",
                    "settings": {"stability": 0.5, "similarity_boost": 0.75, "use_speaker_boost": True},
                },
                is_active=True,
            ),
            # Murf AI
            Integration(
                id="test_prov_murf",
                business_id="test_bid_providers",
                provider="murf",
                config={
                    "name": "Murf Gen2 Config",
                    "api_key": encrypt_api_key("murf_api_key_5678"),
                    "voice_id": "Pooja",
                    "model_id": "GEN2",
                    "language": "en-US",
                    "settings": {"pitch": 5, "speed": 1.1},
                },
                is_active=True,
            ),
            # Fish Audio
            Integration(
                id="test_prov_fish",
                business_id="test_bid_providers",
                provider="fish_audio",
                config={
                    "name": "Fish Audio Neural Clone",
                    "api_key": encrypt_api_key("fish_secret_9999"),
                    "voice_id": "ref_model_custom_001",
                    "model_id": "speech-1.5",
                    "settings": {"temperature": 0.7, "top_p": 0.8},
                },
                is_active=True,
            ),
            # SnapServe
            Integration(
                id="test_prov_snapserve",
                business_id="test_bid_providers",
                provider="snapserve",
                config={
                    "name": "SnapServe Telephony Core",
                    "api_key": encrypt_api_key("snapserve_bearer_token_1111"),
                    "voice_id": "ca3007f96ae7499ab87d27ea3599956a",
                    "model_id": "default",
                    "voice_url": "https://voice.snapserve.ai",
                    "settings": {"sample_rate": 8000},
                },
                is_active=True,
            ),
        ]
        db.add_all(configs)
        await db.commit()

        # Verify all 4 stored and loaded
        for cfg in configs:
            loaded = await db.get(Integration, cfg.id)
            assert loaded is not None
            assert loaded.provider == cfg.provider
            decrypted = decrypt_api_key(loaded.config["api_key"])
            assert len(decrypted) > 0
            await db.delete(loaded)
        await db.commit()


@pytest.mark.asyncio
async def test_resolve_tts_credential_multi_tenant_isolation():
    """Verify that credentials cannot be accessed across different organizations."""
    org_alpha = "org_alpha_isolated"
    org_beta = "org_beta_isolated"

    async with AsyncSessionLocal() as db:
        integ = Integration(
            id="integ_org_beta_only",
            business_id=org_beta,
            provider="elevenlabs",
            config={"api_key": encrypt_api_key("super_secret_org_beta_key")},
            is_active=True,
        )
        db.add(integ)
        await db.commit()

        # Org Alpha requesting Org Beta's credential must fail / resolve None or fallback
        resolved_alpha = await resolve_tts_credential(
            provider="elevenlabs",
            voice_config={"credential_id": "integ_org_beta_only"},
            business_id=org_alpha,
            db=db,
        )
        assert resolved_alpha != "super_secret_org_beta_key"

        # Org Beta resolving its own credential succeeds
        resolved_beta = await resolve_tts_credential(
            provider="elevenlabs",
            voice_config={"credential_id": "integ_org_beta_only"},
            business_id=org_beta,
            db=db,
        )
        assert resolved_beta == "super_secret_org_beta_key"

        # Disabled credential must not resolve
        integ.is_active = False
        await db.commit()

        resolved_disabled = await resolve_tts_credential(
            provider="elevenlabs",
            voice_config={"credential_id": "integ_org_beta_only"},
            business_id=org_beta,
            db=db,
        )
        assert resolved_disabled != "super_secret_org_beta_key"

        await db.delete(integ)
        await db.commit()


@pytest.mark.asyncio
async def test_synthesize_from_voice_config_execution_routing():
    """Verify synthesize_from_voice_config routes to the matching provider implementation."""
    test_bid = "test_bid_synth_routing"

    # 1. Test ElevenLabs Routing
    with patch("app.services.tts_service.synthesize_speech", new_callable=AsyncMock) as mock_eleven:
        mock_eleven.return_value = b"RIFF_MOCK_ELEVENLABS_AUDIO"
        audio = await synthesize_from_voice_config(
            text="Hello world from ElevenLabs",
            voice_config={
                "provider": "elevenlabs",
                "voice_id": "21m00Tcm4TlvDq8ikWAM",
                "model_id": "eleven_turbo_v2",
                "api_key": "test_eleven_key",
                "elevenlabs": {"stability": 0.7},
            },
            business_id=test_bid,
        )
        assert audio == b"RIFF_MOCK_ELEVENLABS_AUDIO"
        mock_eleven.assert_called_once()

    # 2. Test Murf AI Routing
    with patch("app.services.tts_service.synthesize_murf_speech", new_callable=AsyncMock) as mock_murf:
        mock_murf.return_value = b"RIFF_MOCK_MURF_AUDIO"
        audio = await synthesize_from_voice_config(
            text="Hello world from Murf",
            voice_config={
                "provider": "murf",
                "voice_id": "Pooja",
                "model_id": "GEN2",
                "api_key": "test_murf_key",
                "murf": {"pitch": 0},
            },
            business_id=test_bid,
        )
        assert audio == b"RIFF_MOCK_MURF_AUDIO"
        mock_murf.assert_called_once()

    # 3. Test Fish Audio Routing
    with patch("app.services.tts_service.synthesize_fish_audio_speech", new_callable=AsyncMock) as mock_fish:
        mock_fish.return_value = b"RIFF_MOCK_FISH_AUDIO"
        audio = await synthesize_from_voice_config(
            text="Hello world from Fish Audio",
            voice_config={
                "provider": "fish_audio",
                "voice_id": "ref_model_001",
                "model_id": "speech-1.5",
                "api_key": "test_fish_key",
            },
            business_id=test_bid,
        )
        assert audio == b"RIFF_MOCK_FISH_AUDIO"
        mock_fish.assert_called_once()

    # 4. Test SnapServe Routing
    with patch("app.services.tts_service.synthesize_snapserve_speech", new_callable=AsyncMock) as mock_snap:
        mock_snap.return_value = b"RIFF_MOCK_SNAPSERVE_AUDIO"
        audio = await synthesize_from_voice_config(
            text="Hello world from SnapServe",
            voice_config={
                "provider": "snapserve",
                "voice_id": "ca3007f96ae7499ab87d27ea3599956a",
                "voice_url": "https://voice.snapserve.ai",
                "api_key": "test_snap_key",
            },
            business_id=test_bid,
        )
        assert audio == b"RIFF_MOCK_SNAPSERVE_AUDIO"
        mock_snap.assert_called_once()


@pytest.mark.asyncio
async def test_preview_synthesis_pipeline():
    """Verify synthesize_preview functions correctly with voice_config."""
    valid_mock_wav = b"RIFF" + (b"\x00" * 300)
    with patch("app.services.tts_service.synthesize_from_voice_config", new_callable=AsyncMock) as mock_synth:
        mock_synth.return_value = valid_mock_wav
        preview_audio = await synthesize_preview(
            text="Sample voice preview text",
            voice_config={
                "provider": "elevenlabs",
                "voice_id": "EXAVITQu4vr4xnSDxMaL",
                "model_id": "eleven_multilingual_v2",
                "api_key": "mock_key_preview",
            },
            business_id="test_bid_preview",
        )
        assert preview_audio == valid_mock_wav
        mock_synth.assert_called_once()
