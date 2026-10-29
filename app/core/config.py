from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ENV_FILE = Path(__file__).resolve().parent.parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        extra="ignore"
    )

    environment: str = "development"
    secret_key: str = "change-me-super-secret"
    access_token_expire_minutes: int = 43200
    # Publicly reachable base URL for this backend — Twilio calls back to
    # {public_base_url}/api/voice/... . Use an ngrok/cloudflared tunnel URL
    # in local dev; a real domain in production.
    public_base_url: str = "http://localhost:8000"

    razorpay_key_id: str = "rzp_test_TMkmJogQ2Homiz"
    razorpay_key_secret: str = ""

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/appdb"

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/1"
    celery_result_backend: str = "redis://localhost:6379/2"

    llm_provider: str = "gemini"  # gemini | mistral | openai | anthropic
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    mistral_api_key: str = ""
    gemini_model: str = "gemini-3.1-flash-lite"
    gemini_live_model: str = "gemini-3.1-flash-lite"
    gemini_timeout_seconds: float = 8.0
    mistral_model: str = "open-mistral-7b"
    mistral_base_url: str = "https://api.mistral.ai/v1"

    tts_provider: str = "murf"
    tts_model: str = "Falcon"
    snapserve_api_key: str = "sk_live_e87a3f9617177dcfcbb27bf5236a38fa00829cb0235cc43e2a9ebfce4454baed"
    snapserve_voice_id: str = "ca3007f96ae7499ab87d27ea3599956a"
    snapserve_api_url: str = "https://api.snapserve.ai/v1/tts"
    murf_api_key: str = ""
    murf_voice_id: str = "Pooja"
    murf_style: str = "Conversational"
    murf_model: str = "Falcon"
    murf_multi_native_locale: str = "en-IN"
    murf_api_url: str = "https://global.api.murf.ai/v1/speech/stream"

    deepgram_api_key: str = ""
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    sarvam_api_key: str = ""


    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str | None = None
    twilio_enable_recording: bool = False
    
    hold_music_url: str = "https://s3.amazonaws.com/com.twilio.music.classical/MARKOVICHAMP-Borghestral.mp3"
    # SIP Infrastructure Settings (Account credentials & endpoints are stored dynamically in database)
    sip_enabled: bool = True
    sip_inbound_enabled: bool = True
    sip_inbound_port: int = 5060
    sip_rtp_port_min: int = 10000
    sip_rtp_port_max: int = 20000
    sip_host: str = ""
    sip_server: str = ""
    sip_proxy: str = ""
    sip_domain: str = ""
    sip_username: str = ""
    sip_login: str = ""
    sip_password: str = ""
    sip_did_number: str = ""
    sip_port: int = 5060
    sip_dialing_prefix: str = ""
    sip_transport: str = "UDP"
    sip_register_refresh: int = 300
    sip_keep_alive: int = 15
    sip_extension: str = ""
    sip_ws_url: str = ""
    sip_rtp_port: int = 10000
    sip_rtp_ip: str = ""
    sip_local_ip: str = ""
    stun_server: str = "stun:stun.l.google.com:19302"
    turn_server: str = ""
    turn_username: str = ""
    turn_password: str = ""

    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_phone_number: str = "+1 (555) 159-5909"
    whatsapp_verify_token: str = "callzenza_whatsapp_verify_token"

    # Email & SMTP Configuration
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_secure: bool = False
    smtp_encryption: str = "TLS"

    sendgrid_api_key: str = ""
    email_from: str = "noreply@example.com"

    resend_api_key: str = ""
    resend_from_email: str = "noreply@callzenza.com"
    resend_from_name: str = "CallZenza"

    crm_provider: str = "salesforce"
    crm_api_key: str = ""

    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_s3_bucket: str = ""
    aws_region: str = "us-east-1"

    elasticsearch_url: str = "http://elasticsearch:9200"

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    hubspot_client_id: str = ""
    hubspot_client_secret: str = ""
    salesforce_client_id: str = ""
    salesforce_client_secret: str = ""
    zoho_client_id: str = ""
    zoho_client_secret: str = ""
    rag_similarity_threshold: float = 0.25
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    vector_db_type: str = "faiss"
    rag_llm_model: str = "gemini-2.0-flash"
    rag_llm_provider: str = "gemini"
    rag_llm_api_base: str = ""
    rag_top_k: int = 5
    rag_chunk_size: int = 500
    rag_chunk_overlap: int = 50

    # Multi-tenant SaaS Cluster & Routing Configuration
    cluster_id: str = "default"
    base_domain: str = "callzenza.com"
    frontend_url: str = "http://localhost:3000"

    # Super Admin Bootstrap Configuration
    super_admin_email: str = "superadmin@callzenza.com"
    super_admin_initial_password: str = "superadmin123"

    # Feature Flags
    enable_ai_to_human_transfer: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()