"""Pydantic schemas for Multi-Tenant SIP Configuration."""
from datetime import datetime
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


class SIPAccountBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, description="Display name for this SIP account")
    username: str = Field(..., min_length=1, max_length=128, description="SIP / PJSIP username")
    auth_username: Optional[str] = Field(None, max_length=128, description="Auth username if different from username")
    domain: Optional[str] = Field(None, max_length=255, description="SIP Domain / Realm")
    host: str = Field(..., min_length=1, max_length=255, description="SIP Server hostname or IP address")
    port: int = Field(5060, ge=1, le=65535, description="SIP Port (default: 5060)")
    transport: str = Field("udp", description="Transport protocol: udp, tcp, tls")
    outbound_proxy: Optional[str] = Field(None, max_length=255, description="Outbound proxy if required")
    caller_id: Optional[str] = Field(None, max_length=64, description="Default Caller ID / Outbound CLI")
    direction: str = Field("both", description="Allowed call direction: outbound, inbound, both")
    enabled: bool = Field(True, description="Whether this account is enabled for calling")
    max_channels: int = Field(10, ge=1, le=1000, description="Max concurrent channels allowed")
    settings: Optional[Dict[str, Any]] = Field(default_factory=dict, description="Additional provider-specific flags")
    sip_server: Optional[str] = Field(None, description="SIP Server hostname or IP")
    sip_login: Optional[str] = Field(None, description="SIP Login user/auth")
    sip_public_address: Optional[str] = Field("Auto", description="SIP Public IP/FQDN or Auto")
    voicemail_number: Optional[str] = Field(None, description="SIP Voicemail extension/number")
    dial_plan: Optional[str] = Field(None, description="SIP Dial Plan pattern")
    webhook_base_url: Optional[str] = Field(None, description="Webhook Base URL")
    additional_dids: Optional[List[str]] = Field(default_factory=list, description="Additional DIDs mapped to this SIP trunk")
    hide_caller_id: Optional[bool] = Field(False, description="Hide Caller ID")
    media_encryption: Optional[bool] = Field(False, description="Enable SRTP/TLS media encryption")
    publish_presence: Optional[bool] = Field(False, description="Publish SIP presence")
    allow_ip_rewrite: Optional[bool] = Field(False, description="Allow IP rewrite for NAT")
    ice: Optional[bool] = Field(False, description="Enable ICE / STUN candidates")
    disable_session_timers: Optional[bool] = Field(False, description="Disable RFC 4028 session timers")
    use_auth_user_in_from: Optional[bool] = Field(False, description="Use SIP username in From URI for Asterisk / strict PBX authentication")
    extension: Optional[str] = Field(None, description="Internal PBX SIP extension (e.g. 1001)")
    dialing_prefix: Optional[str] = Field(None, description="Outbound dialing prefix prepended before destination numbers")
    display_name: Optional[str] = Field(None, description="Caller ID Display Name (e.g. Sales Team)")
    refresh_interval: Optional[int] = Field(1800, description="SIP registration refresh interval in seconds")
    keep_alive_interval: Optional[int] = Field(20, description="NAT keep-alive OPTIONS ping interval in seconds")
    local_ip: Optional[str] = Field(None, description="Local SIP binding / signaling IP")
    rtp_ip: Optional[str] = Field(None, description="Advertised RTP media IP")
    rtp_port: Optional[int] = Field(None, description="RTP base port")
    stun_server: Optional[str] = Field(None, description="STUN server for NAT traversal")
    sip_ws_url: Optional[str] = Field(None, description="SIP WebSocket URL for WebRTC")
    is_default: bool = Field(False, description="Whether this account is the default outbound SIP route")

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: str) -> str:
        cleaned = (v or "").strip()
        if not cleaned:
            raise ValueError("SIP host cannot be empty.")
        lowered = cleaned.lower()
        if (
            lowered in ("host", "test", "dummy", "placeholder", "example")
            or lowered.startswith(("dummy.", "test.", "placeholder.", "example."))
            or lowered.endswith((".local", ".test", ".example", ".invalid"))
        ):
            raise ValueError(f"'{cleaned}' is a placeholder hostname and cannot be used as a configured SIP endpoint.")
        return cleaned

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: str) -> str:
        cleaned = (v or "udp").strip().lower()
        if cleaned not in ("udp", "tcp", "tls"):
            raise ValueError("Transport must be one of: 'udp', 'tcp', 'tls'")
        return cleaned

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: str) -> str:
        cleaned = (v or "both").strip().lower()
        if cleaned not in ("outbound", "inbound", "both"):
            raise ValueError("Direction must be one of: 'outbound', 'inbound', 'both'")
        return cleaned


class SIPAccountCreate(SIPAccountBase):
    password: Optional[str] = Field(None, description="Plaintext SIP secret to be securely encrypted at rest")
    organization_id: Optional[str] = Field(None, description="Target organization/tenant ID (Admin assignment)")


class SIPAccountUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=128)
    organization_id: Optional[str] = Field(None, description="Target organization/tenant ID (Admin reassignment)")
    username: Optional[str] = Field(None, min_length=1, max_length=128)
    password: Optional[str] = Field(None, description="New secret, or null to keep current")
    auth_username: Optional[str] = None
    domain: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = Field(None, ge=1, le=65535)
    transport: Optional[str] = None
    outbound_proxy: Optional[str] = None
    caller_id: Optional[str] = None
    direction: Optional[str] = None
    enabled: Optional[bool] = None
    max_channels: Optional[int] = Field(None, ge=1, le=1000)
    settings: Optional[Dict[str, Any]] = None
    sip_server: Optional[str] = None
    sip_login: Optional[str] = None
    sip_public_address: Optional[str] = None
    voicemail_number: Optional[str] = None
    dial_plan: Optional[str] = None
    webhook_base_url: Optional[str] = None
    additional_dids: Optional[List[str]] = None
    hide_caller_id: Optional[bool] = None
    media_encryption: Optional[bool] = None
    publish_presence: Optional[bool] = None
    allow_ip_rewrite: Optional[bool] = None
    ice: Optional[bool] = None
    disable_session_timers: Optional[bool] = None
    use_auth_user_in_from: Optional[bool] = None
    extension: Optional[str] = None
    dialing_prefix: Optional[str] = None
    display_name: Optional[str] = None
    refresh_interval: Optional[int] = None
    keep_alive_interval: Optional[int] = None
    local_ip: Optional[str] = None
    rtp_ip: Optional[str] = None
    rtp_port: Optional[int] = None
    stun_server: Optional[str] = None
    sip_ws_url: Optional[str] = None
    is_default: Optional[bool] = None

    @field_validator("host")
    @classmethod
    def validate_host(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("SIP host cannot be empty.")
        if cleaned.lower() in ("host", "test", "dummy", "placeholder", "example"):
            raise ValueError(f"'{cleaned}' is a placeholder hostname and cannot be used as a configured SIP endpoint.")
        return cleaned

    @field_validator("transport")
    @classmethod
    def validate_transport(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip().lower()
        if cleaned not in ("udp", "tcp", "tls"):
            raise ValueError("Transport must be one of: 'udp', 'tcp', 'tls'")
        return cleaned

    @field_validator("direction")
    @classmethod
    def validate_direction(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        cleaned = v.strip().lower()
        if cleaned not in ("outbound", "inbound", "both"):
            raise ValueError("Direction must be one of: 'outbound', 'inbound', 'both'")
        return cleaned


class SIPAccountDIDOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    phone_number: str
    friendly_name: Optional[str] = None
    status: Optional[str] = "ACTIVE"
    is_outbound_caller_id: Optional[bool] = False
    sip_account_id: Optional[str] = None
    organization_id: Optional[str] = None
    created_at: Optional[datetime] = None


class SIPAccountDIDAssign(BaseModel):
    did_ids: List[str] = Field(..., description="List of DID IDs to assign to this SIP account")


class SIPAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    organization_id: str
    organization_name: Optional[str] = None
    name: str
    username: str
    auth_username: Optional[str] = None
    domain: Optional[str] = None
    host: str
    port: int
    transport: str
    outbound_proxy: Optional[str] = None
    caller_id: Optional[str] = None
    direction: str
    enabled: bool
    registration_status: str
    last_health_status: Optional[str] = None
    last_health_latency_ms: Optional[float] = None
    last_health_checked_at: Optional[datetime] = None
    last_error_message: Optional[str] = None
    max_channels: int
    has_password: bool = False
    masked_password: str = "••••••••"
    assigned_campaigns_count: int = 0
    dids: List[SIPAccountDIDOut] = Field(default_factory=list)
    settings: Optional[Dict[str, Any]] = None
    sip_server: Optional[str] = None
    sip_login: Optional[str] = None
    sip_public_address: Optional[str] = None
    voicemail_number: Optional[str] = None
    dial_plan: Optional[str] = None
    webhook_base_url: Optional[str] = None
    additional_dids: List[str] = Field(default_factory=list)
    hide_caller_id: bool = False
    media_encryption: bool = False
    publish_presence: bool = False
    allow_ip_rewrite: bool = False
    ice: bool = False
    disable_session_timers: bool = False
    use_auth_user_in_from: bool = False
    extension: Optional[str] = None
    dialing_prefix: Optional[str] = None
    display_name: Optional[str] = None
    refresh_interval: Optional[int] = 1800
    keep_alive_interval: Optional[int] = 20
    local_ip: Optional[str] = None
    rtp_ip: Optional[str] = None
    rtp_port: Optional[int] = None
    stun_server: Optional[str] = None
    sip_ws_url: Optional[str] = None
    is_default: bool = False
    error_code: Optional[str] = None
    sip_status_code: Optional[int] = None
    sip_status_reason: Optional[str] = None
    check_type: Optional[str] = None
    suggested_action: Optional[str] = None
    retryable: Optional[bool] = None
    registration_diagnostics: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SIPAccountTestResponse(BaseModel):
    account_id: str
    reachable: bool
    latency_ms: float
    transport: str
    check_type: Optional[str] = "SIP_OPTIONS"
    sip_status_code: Optional[int] = None
    sip_status_reason: Optional[str] = None
    dns_resolved_ip: Optional[str] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    suggested_action: Optional[str] = None
    retryable: Optional[bool] = None
    timestamp: datetime


class SIPAccountRegisterResponse(BaseModel):
    account_id: str
    status: str
    message: str
    is_registered: bool
    last_error: Optional[str] = None
    error_code: Optional[str] = None
    sip_status_code: Optional[int] = None
    retryable: bool = False
    suggested_action: Optional[str] = None
    last_checked_at: Optional[str] = None
    timestamp: datetime


class SIPAccountToggleStatus(BaseModel):
    enabled: bool
