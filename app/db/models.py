import uuid
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# RBAC roles (module 6). Simplified to a role hierarchy rather than a full
# per-permission matrix, to keep this shippable — see require_role() in
# core/security.py for how these gate endpoints.
# ---------------------------------------------------------------------------
ROLES = [
    "SUPER_ADMIN",
    "BUSINESS_OWNER",
    "ADMIN",
    "SUPERVISOR",
    "VOICE_AGENT",
    "SALES_MANAGER",
    "SALES_EXPERT",
    "AGENT_MANAGER",
    "VIEWER",
    "AI_AGENT_PORTAL_USER",
]


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    country: Mapped[str | None] = mapped_column(String(100), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str] = mapped_column(String(50), default="VIEWER")
    is_active: Mapped[bool] = mapped_column(default=True)
    business_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("businesses.id"), nullable=True, index=True
    )
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    plain_password: Mapped[str | None] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(30), default="AVAILABLE")
    sip_extension: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Business(Base):
    """Module 7 — multi-tenant business/tenant."""

    __tablename__ = "businesses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True, index=True)
    industry: Mapped[str] = mapped_column(String(100), default="")
    owner_user_id: Mapped[str] = mapped_column(String(36), nullable=False)
    twilio_number: Mapped[str] = mapped_column(String(50), default="", server_default="")  # per-tenant Twilio number for inbound routing
    is_active: Mapped[bool] = mapped_column(default=True)
    status: Mapped[str] = mapped_column(String(50), default="active", index=True)  # active, pending_verification, suspended
    billing_company_name: Mapped[str | None] = mapped_column(String(255), nullable=True, default="", server_default="")
    billing_email: Mapped[str | None] = mapped_column(String(255), nullable=True, default="", server_default="")
    billing_address: Mapped[str | None] = mapped_column(Text, nullable=True, default="", server_default="")
    billing_tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True, default="", server_default="")
    wallet_balance: Mapped[float] = mapped_column(Float, default=0.0)
    current_plan: Mapped[str] = mapped_column(String(50), default="Free Plan")
    registration_source: Mapped[str] = mapped_column(String(50), default="UNKNOWN", index=True)  # SUPER_ADMIN_CREATED, SELF_REGISTERED
    permissions: Mapped[dict] = mapped_column(JSON, default=dict)
    
    # Subdomain and Cluster Distribution
    subdomain: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    custom_domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    cluster_id: Mapped[str] = mapped_column(String(50), default="default", server_default="default", index=True)
    server_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    region: Mapped[str] = mapped_column(String(50), default="us-east", server_default="us-east")

    # Data Isolation Strategy
    isolation_strategy: Mapped[str] = mapped_column(String(50), default="SHARED_DATABASE", server_default="SHARED_DATABASE")
    database_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    schema_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class EmailVerificationToken(Base):
    """Cryptographically secure email activation tokens for organization signup."""

    __tablename__ = "email_verification_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("businesses.id"), index=True, nullable=True)
    business_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)  # PENDING, VERIFIED, EXPIRED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class UserInvitation(Base):
    """Team member invitation tokens for organization admin user invites."""

    __tablename__ = "user_invitations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("businesses.id"), index=True, nullable=True)
    business_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    user_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(50), default="USER")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)  # PENDING, ACCEPTED, EXPIRED, REVOKED
    invited_by_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PasswordResetToken(Base):
    """Secure one-time tokens for self-serve password recovery."""

    __tablename__ = "password_reset_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    is_used: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", index=True)  # PENDING, USED, EXPIRED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AIAgent(Base):
    """Module 8 — AI Employee."""

    __tablename__ = "ai_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[str] = mapped_column(String(50), default="SALES")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    voice_config_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True, default=None)
    voice_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True, default=None)
    voice_provider: Mapped[str] = mapped_column(String(50), default="elevenlabs")
    voice_id: Mapped[str] = mapped_column(String(100), default="21m00Tcm4TlvDq8ikWAM")  # Default ElevenLabs Rachel
    voice_stability: Mapped[float] = mapped_column(Float, default=0.5)  # 0-1, lower = more expressive/variable
    voice_similarity_boost: Mapped[float] = mapped_column(Float, default=0.75)  # 0-1, higher = closer to source voice
    voice_model_id: Mapped[str] = mapped_column(String(50), default="eleven_multilingual_v2")
    language: Mapped[str] = mapped_column(String(10), default="en")
    system_prompt: Mapped[str] = mapped_column(Text, default="You are a helpful AI sales agent.")
    qualification_prompt: Mapped[str] = mapped_column(Text, default="")
    working_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    transfer_enabled: Mapped[bool] = mapped_column(default=True)
    transfer_number: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class QualificationRule(Base):
    """Module 12 — configurable qualification weights + thresholds per business."""

    __tablename__ = "qualification_rules"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True, unique=True)
    weights: Mapped[dict] = mapped_column(
        JSON,
        default=lambda: {
            "icp": 30,
            "budget": 25,
            "timeline": 20,
            "requirement": 15,
            "decision_maker": 10,
        },
    )
    unqualified_max: Mapped[int] = mapped_column(Integer, default=39)
    nurture_max: Mapped[int] = mapped_column(Integer, default=69)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class LeadGroup(Base):
    __tablename__ = "lead_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    lead_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default="IDLE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Lead(Base):
    """Module 9 — Lead."""

    __tablename__ = "leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("lead_groups.id", ondelete="CASCADE"), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(50), default="MANUAL")
    created_by_role: Mapped[str] = mapped_column(String(20), default="USER", server_default="USER")
    first_name: Mapped[str] = mapped_column(String(100), default="")
    last_name: Mapped[str] = mapped_column(String(100), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    phone: Mapped[str] = mapped_column(String(50), default="")
    company: Mapped[str] = mapped_column(String(255), default="")
    job_title: Mapped[str] = mapped_column(String(255), default="")
    requirement: Mapped[str] = mapped_column(Text, default="")
    budget: Mapped[str] = mapped_column(String(100), default="")
    timeline: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    score: Mapped[int] = mapped_column(Integer, default=0)
    qualification_status: Mapped[str] = mapped_column(String(20), default="UNQUALIFIED")
    ai_summary: Mapped[str] = mapped_column(Text, default="")
    call_status: Mapped[str | None] = mapped_column(String(50), default="NEW", nullable=True)
    call_date_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    call_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    twilio_call_sid: Mapped[str | None] = mapped_column(String(50), nullable=True)
    call_result: Mapped[str | None] = mapped_column(String(255), default="—", nullable=True)
    assigned_expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id"), nullable=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id"), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    assigned_expert: Mapped[Optional["Expert"]] = relationship("Expert", foreign_keys=[assigned_expert_id], lazy="selectin")

    @property
    def agent_name(self) -> str | None:
        try:
            exp = getattr(self, "assigned_expert", None)
            if exp:
                return exp.name
        except Exception:
            pass
        return None

    @property
    def assigned_agent_name(self) -> str | None:
        try:
            exp = getattr(self, "assigned_expert", None)
            if exp:
                return exp.name
        except Exception:
            pass
        return None


class Conversation(Base):
    """Module 21 — Conversation (voice/chat/sms/whatsapp/email all land here)."""

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default="LIVE_CHAT")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Message(Base):
    """Module 11 — every conversation turn."""

    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("conversations.id"), index=True)
    sender_type: Mapped[str] = mapped_column(String(20))
    sender_id: Mapped[str] = mapped_column(String(36), default="")
    message_type: Mapped[str] = mapped_column(String(20), default="TEXT")
    content: Mapped[str] = mapped_column(Text, default="")
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Expert(Base):
    """Module 17 — human sales expert."""

    __tablename__ = "experts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), default="")
    skills: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="AVAILABLE")
    active_lead_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_dial_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class LeadAssignment(Base):
    """Module 17/16 — record of a lead being handed to an expert."""

    __tablename__ = "lead_assignments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    rag_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    expert_id: Mapped[str] = mapped_column(String(36), ForeignKey("experts.id"), index=True)
    previous_expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id", ondelete="SET NULL"), nullable=True, index=True)
    strategy: Mapped[str] = mapped_column(String(50), default="LEAST_LOADED")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    trigger_source: Mapped[str] = mapped_column(String(50), default="TALK_TO_AGENT")
    ai_agent_name: Mapped[str] = mapped_column(String(255), default="")
    qualification_stage: Mapped[str] = mapped_column(String(100), default="")
    transcript: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    context: Mapped[dict] = mapped_column(JSON, default=dict)
    assigned_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class AgentAssignmentQueue(Base):
    """Record of leads waiting for available human voice agents."""

    __tablename__ = "agent_assignment_queue"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    requested_expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id", ondelete="SET NULL"), nullable=True, index=True)
    trigger_source: Mapped[str] = mapped_column(String(50), default="TALK_TO_AGENT")
    reason: Mapped[str] = mapped_column(String(255), default="NO_AGENT_AVAILABLE")
    status: Mapped[str] = mapped_column(String(20), default="WAITING", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    call_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    call_completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    call_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    call_session_id: Mapped[str | None] = mapped_column(String(255), nullable=True)



class Deal(Base):
    """Module 18 — sales pipeline."""

    __tablename__ = "deals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    value: Mapped[float] = mapped_column(Float, default=0)
    currency: Mapped[str] = mapped_column(String(10), default="USD")
    stage: Mapped[str] = mapped_column(String(30), default="NEW", index=True)
    probability: Mapped[int] = mapped_column(Integer, default=10)
    expected_close_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Appointment(Base):
    """Module 19 — scheduling."""

    __tablename__ = "appointments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id"), nullable=True)
    expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id"), nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="SCHEDULED")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class SIPAccount(Base):
    """
    Multi-Tenant SIP Account (RFC 3261).
    Owned by an organization/business for outbound and inbound Asterisk/PJSIP telephony.
    Assigned directly to campaigns (AI Agent & Voice Agent) and traced on call records.
    """
    __tablename__ = "sip_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    username: Mapped[str] = mapped_column(String(150), nullable=False)
    auth_username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True)
    host: Mapped[str] = mapped_column(String(255), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=5060)
    transport: Mapped[str] = mapped_column(String(20), default="UDP")  # UDP, TCP, TLS
    outbound_proxy: Mapped[str | None] = mapped_column(String(255), nullable=True)
    caller_id: Mapped[str | None] = mapped_column(String(60), nullable=True)
    direction: Mapped[str] = mapped_column(String(30), default="BIDIRECTIONAL")  # OUTBOUND, INBOUND, BIDIRECTIONAL
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    registration_status: Mapped[str] = mapped_column(String(50), default="UNREGISTERED")  # REGISTERED, UNREGISTERED, PEER_ACTIVE, FAILED
    last_health_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN")  # HEALTHY, DEGRADED, UNREACHABLE, UNKNOWN
    last_health_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    max_channels: Mapped[int] = mapped_column(Integer, default=30)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    dids: Mapped[List["DIDNumber"]] = relationship("DIDNumber", foreign_keys="[DIDNumber.sip_account_id]", back_populates="sip_account", lazy="selectin")

    def __init__(self, **kwargs):
        password = kwargs.pop("password", None)
        business_id = kwargs.pop("business_id", None)
        if business_id is not None:
            kwargs["organization_id"] = business_id
        
        # Pop extended properties to assign after super init
        extension = kwargs.pop("extension", None)
        dialing_prefix = kwargs.pop("dialing_prefix", None)
        display_name = kwargs.pop("display_name", None)
        refresh_interval = kwargs.pop("refresh_interval", None)
        keep_alive_interval = kwargs.pop("keep_alive_interval", None)
        local_ip = kwargs.pop("local_ip", None)
        rtp_ip = kwargs.pop("rtp_ip", None)
        rtp_port = kwargs.pop("rtp_port", None)
        stun_server = kwargs.pop("stun_server", None)
        sip_ws_url = kwargs.pop("sip_ws_url", None)
        is_default = kwargs.pop("is_default", None)

        super().__init__(**kwargs)

        if password:
            self.password = password
        if extension is not None:
            self.extension = extension
        if dialing_prefix is not None:
            self.dialing_prefix = dialing_prefix
        if display_name is not None:
            self.display_name = display_name
        if refresh_interval is not None:
            self.refresh_interval = refresh_interval
        if keep_alive_interval is not None:
            self.keep_alive_interval = keep_alive_interval
        if local_ip is not None:
            self.local_ip = local_ip
        if rtp_ip is not None:
            self.rtp_ip = rtp_ip
        if rtp_port is not None:
            self.rtp_port = rtp_port
        if stun_server is not None:
            self.stun_server = stun_server
        if sip_ws_url is not None:
            self.sip_ws_url = sip_ws_url
        if is_default is not None:
            self.is_default = is_default

    @property
    def password(self) -> str:
        from app.services.sip_account_service import decrypt_sip_secret
        return decrypt_sip_secret(self.password_hash or "")

    @password.setter
    def password(self, val: str | None):
        from app.services.sip_account_service import encrypt_sip_secret
        if val:
            self.password_hash = encrypt_sip_secret(val)
        else:
            self.password_hash = None

    @property
    def encrypted_password(self) -> str | None:
        return self.password_hash

    @encrypted_password.setter
    def encrypted_password(self, val: str | None):
        self.password_hash = val

    @property
    def business_id(self) -> str:
        return self.organization_id

    @business_id.setter
    def business_id(self, val: str):
        self.organization_id = val

    @property
    def extension(self) -> str | None:
        return (self.settings or {}).get("extension")

    @extension.setter
    def extension(self, val: str | None):
        s = dict(self.settings or {})
        s["extension"] = val
        self.settings = s

    @property
    def dialing_prefix(self) -> str | None:
        return (self.settings or {}).get("dialing_prefix")

    @dialing_prefix.setter
    def dialing_prefix(self, val: str | None):
        s = dict(self.settings or {})
        s["dialing_prefix"] = val
        self.settings = s

    @property
    def display_name(self) -> str | None:
        return (self.settings or {}).get("display_name")

    @display_name.setter
    def display_name(self, val: str | None):
        s = dict(self.settings or {})
        s["display_name"] = val
        self.settings = s

    @property
    def refresh_interval(self) -> int:
        val = (self.settings or {}).get("refresh_interval")
        return int(val) if val is not None else 3600

    @refresh_interval.setter
    def refresh_interval(self, val: int):
        s = dict(self.settings or {})
        s["refresh_interval"] = int(val)
        self.settings = s

    @property
    def keep_alive_interval(self) -> int:
        val = (self.settings or {}).get("keep_alive_interval")
        return int(val) if val is not None else 30

    @keep_alive_interval.setter
    def keep_alive_interval(self, val: int):
        s = dict(self.settings or {})
        s["keep_alive_interval"] = int(val)
        self.settings = s

    @property
    def local_ip(self) -> str | None:
        return (self.settings or {}).get("local_ip")

    @local_ip.setter
    def local_ip(self, val: str | None):
        s = dict(self.settings or {})
        s["local_ip"] = val
        self.settings = s

    @property
    def rtp_ip(self) -> str | None:
        return (self.settings or {}).get("rtp_ip")

    @rtp_ip.setter
    def rtp_ip(self, val: str | None):
        s = dict(self.settings or {})
        s["rtp_ip"] = val
        self.settings = s

    @property
    def rtp_port(self) -> int | None:
        val = (self.settings or {}).get("rtp_port")
        return int(val) if val is not None else None

    @rtp_port.setter
    def rtp_port(self, val: int | None):
        s = dict(self.settings or {})
        s["rtp_port"] = int(val) if val is not None else None
        self.settings = s

    @property
    def stun_server(self) -> str | None:
        return (self.settings or {}).get("stun_server")

    @stun_server.setter
    def stun_server(self, val: str | None):
        s = dict(self.settings or {})
        s["stun_server"] = val
        self.settings = s

    @property
    def sip_ws_url(self) -> str | None:
        return (self.settings or {}).get("sip_ws_url")

    @sip_ws_url.setter
    def sip_ws_url(self, val: str | None):
        s = dict(self.settings or {})
        s["sip_ws_url"] = val
        self.settings = s

    @property
    def is_default(self) -> bool:
        return bool((self.settings or {}).get("is_default", False))

    @is_default.setter
    def is_default(self, val: bool):
        s = dict(self.settings or {})
        s["is_default"] = bool(val)
        self.settings = s


class Call(Base):
    """Module 14 — voice call record."""

    __tablename__ = "calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    sip_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    client_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("conversations.id"), nullable=True)
    call_sid: Mapped[str] = mapped_column(String(64), default="", index=True)
    conference_sid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="OUTBOUND")  # OUTBOUND/INBOUND
    agent_type: Mapped[str] = mapped_column(String(50), default="AI_VOICE")  # AI_VOICE / HUMAN
    client_name: Mapped[str] = mapped_column(String(255), default="")
    client_phone: Mapped[str] = mapped_column(String(50), default="")
    from_number: Mapped[str] = mapped_column(String(50), default="")
    to_number: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(50), default="QUEUED", index=True)
    # QUEUED/RINGING/IN_PROGRESS/AI_HANDLING/TRANSFER_REQUESTED/HUMAN_HANDLING/QUALIFIED/NOT_QUALIFIED/CLIENT_ADDING/CONFERENCE_3WAY_ACTIVE/CLIENT_ADD_FAILED/COMPLETED/FAILED/NO_ANSWER/BUSY
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    initiated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=_now)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    client_duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    recording_url: Mapped[str] = mapped_column(String(500), default="")
    call_result: Mapped[str | None] = mapped_column(String(255), default="—", nullable=True)
    transcript: Mapped[str] = mapped_column(Text, default="")
    turn_count: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    sip_extension: Mapped[str | None] = mapped_column(String(50), nullable=True, index=True)
    hangup_reason: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

    @property
    def tenant_id(self) -> str:
        return self.business_id

    @tenant_id.setter
    def tenant_id(self, val: str):
        self.business_id = val

    @property
    def caller_number(self) -> str:
        return self.from_number

    @caller_number.setter
    def caller_number(self, val: str):
        self.from_number = val

    @property
    def destination_number(self) -> str:
        return self.to_number

    @destination_number.setter
    def destination_number(self, val: str):
        self.to_number = val

    @property
    def started_at(self) -> datetime | None:
        return self.initiated_at

    @started_at.setter
    def started_at(self, val: datetime | None):
        self.initiated_at = val

    @property
    def notes(self) -> str:
        return self.call_result or ""

    @notes.setter
    def notes(self, val: str):
        self.call_result = str(val)[:255] if val else ""

    @property
    def answered_at(self) -> datetime | None:
        return self.connected_at

    @answered_at.setter
    def answered_at(self, val: datetime | None):
        self.connected_at = val

    @property
    def duration(self) -> int:
        return self.duration_seconds

    @duration.setter
    def duration(self, val: int):
        self.duration_seconds = val


class AuditLog(Base):
    """Module 27 — security/audit trail."""

    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(50), default="")
    entity_id: Mapped[str] = mapped_column(String(36), default="")
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="IDLE")  # IDLE, RUNNING, PAUSED, COMPLETED
    agent_type: Mapped[str] = mapped_column(String(50), default="AI_VOICE")  # AI_VOICE / HUMAN
    agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id"), nullable=True)
    voice_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id"), nullable=True)
    lead_group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("lead_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    client_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True)
    client_name: Mapped[str] = mapped_column(String(255), default="")
    client_phone: Mapped[str] = mapped_column(String(50), default="")
    twilio_number: Mapped[str] = mapped_column(String(50), default="")
    carrier_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    trunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    caller_did_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    sip_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    schedule: Mapped[dict] = mapped_column(JSON, default=dict)
    voice_config: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_dial_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_dial_ratio: Mapped[int] = mapped_column(Integer, default=1)
    
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CampaignLead(Base):
    __tablename__ = "campaign_leads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    assigned_expert_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="PENDING")  # PENDING, RESERVED, DIALING, RINGING, CONNECTED, IN_CALL, COMPLETED, FAILED, NO_ANSWER, BUSY, CANCELLED, RETRY_SCHEDULED
    call_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("calls.id"), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CallMessage(Base):
    __tablename__ = "call_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id"), index=True)
    sender_type: Mapped[str] = mapped_column(String(20))  # AGENT, CUSTOMER
    content: Mapped[str] = mapped_column(Text, default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Transcript(Base):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id"), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    speaker: Mapped[str] = mapped_column(String(50), default="UNKNOWN")  # AGENT, CUSTOMER
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Integration(Base):
    __tablename__ = "integrations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    provider: Mapped[str] = mapped_column(String(50))  # twilio, elevenlabs, gemini, whatsapp, etc.
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id"), index=True)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[str] = mapped_column(String(50), default="INFO")  # INFO, CALL_COMPLETED, LEAD_QUALIFIED, DEMO_REQUEST
    reference_id: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DemoRequest(Base):
    __tablename__ = "demo_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    organization_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(50), default="NEW", index=True)  # NEW, CONTACTED, SCHEDULED, COMPLETED, ARCHIVED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class KnowledgeLibrary(Base):
    __tablename__ = "knowledge_library"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("agent_sessions.id", ondelete="SET NULL"), nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(String(50), default="v1", nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class KnowledgeSource(Base):
    __tablename__ = "knowledge_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_library.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)


class KnowledgeMetadata(Base):
    __tablename__ = "knowledge_metadata"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    knowledge_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_library.id", ondelete="CASCADE"), nullable=False, index=True)
    meta_key: Mapped[str] = mapped_column(String(100), nullable=False)
    meta_value: Mapped[str | None] = mapped_column(Text, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    config_key: Mapped[str] = mapped_column(String(100), unique=True, index=True, nullable=False)
    config_value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DBInvoice(Base):
    __tablename__ = "db_invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    invoice_number: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    date: Mapped[str] = mapped_column(String(50), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(20), default="Paid")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class MgmtAgent(Base):
    __tablename__ = "mgmt_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=True)
    login_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class MgmtVoiceAgent(Base):
    __tablename__ = "mgmt_voice_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("mgmt_agents.id", ondelete="CASCADE"), index=True, nullable=False)
    voice_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class MgmtAIAgent(Base):
    __tablename__ = "mgmt_ai_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("mgmt_agents.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class MgmtAIAgentUser(Base):
    __tablename__ = "mgmt_ai_agent_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    ai_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("mgmt_ai_agents.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)


class MgmtVoiceAgentUser(Base):
    __tablename__ = "mgmt_voice_agent_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    voice_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("mgmt_voice_agents.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)


class ClientConfig(Base):
    """Client configurations for slots D1, D2, D3 per business."""

    __tablename__ = "client_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str] = mapped_column(String(10), nullable=False, index=True)  # D1, D2, D3
    client_id: Mapped[Optional[str]] = mapped_column(String(36), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True)
    client_name: Mapped[str] = mapped_column(String(255), default="")
    client_phone: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ParkedConference(Base):
    """Parked conference participants for a specific call."""

    __tablename__ = "parked_conferences"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id", ondelete="CASCADE"), unique=True, index=True)
    D1_phone: Mapped[str] = mapped_column(String(50), default="")
    D2_phone: Mapped[str] = mapped_column(String(50), default="")
    D3_phone: Mapped[str] = mapped_column(String(50), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

class SessionState(Base):
    __tablename__ = "session_states"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class DialerRun(Base):
    __tablename__ = "dialer_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(50), default="RUNNING")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    paused_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DialJob(Base):
    __tablename__ = "dial_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    dialer_run_id: Mapped[str] = mapped_column(String(36), ForeignKey("dialer_runs.id", ondelete="CASCADE"), index=True)
    campaign_id: Mapped[str] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("calls.id", ondelete="SET NULL"), nullable=True, index=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(50), default="RESERVED")
    reserved_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Client(Base):
    """Centralized Clients module for Admin and Agents."""

    __tablename__ = "clients"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company_name: Mapped[str] = mapped_column(String(255), default="")
    contact_number: Mapped[str] = mapped_column(String(50), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    address: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(50), default="Active")
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("lead_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)



class ParkedCall(Base):
    """Call hold / parked calls tracking."""

    __tablename__ = "parked_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    slot: Mapped[str | None] = mapped_column(String(10), nullable=True)  # D1, D2, D3
    hold_reason: Mapped[str] = mapped_column(String(255), default="PARKED")
    status: Mapped[str] = mapped_column(String(20), default="ON_HOLD", index=True)  # ON_HOLD, RESUMED, ENDED, TRANSFERRED
    hold_start_time: Mapped[datetime] = mapped_column(DateTime, default=_now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class CallTransfer(Base):
    """3-Way Call Transfers tracking."""

    __tablename__ = "call_transfers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    client_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True)
    from_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    to_agent_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    transfer_type: Mapped[str] = mapped_column(String(50), default="three_way")  # three_way, blind
    status: Mapped[str] = mapped_column(String(50), default="PENDING", index=True)
    # PENDING, TARGET_RINGING, TARGET_CONNECTED, THREE_WAY_ACTIVE, COMPLETED, CANCELLED, REJECTED, NO_ANSWER
    connected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    disconnected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CallParticipant(Base):
    """Tracks participants present on a call session."""

    __tablename__ = "call_participants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    call_id: Mapped[str] = mapped_column(String(36), ForeignKey("calls.id", ondelete="CASCADE"), index=True)
    participant_type: Mapped[str] = mapped_column(String(20))  # CUSTOMER, AGENT, CLIENT
    participant_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    phone_number: Mapped[str] = mapped_column(String(50), default="")
    status: Mapped[str] = mapped_column(String(50), default="CONNECTED")
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    left_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    barge_call_sid: Mapped[str | None] = mapped_column(String(100), nullable=True)


class RecycleList(Base):
    """Module: Lead Recycle Lists grouped from Call Dispositions."""

    __tablename__ = "recycle_lists"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    icon: Mapped[str] = mapped_column(String(20), default="↻")
    status: Mapped[str] = mapped_column(String(50), default="READY TO RECYCLE")
    status_badge: Mapped[str] = mapped_column(String(20), default="b-w")
    source_disposition: Mapped[str] = mapped_column(String(100), default="Call Back", index=True)
    source_campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    source_campaign_name: Mapped[str] = mapped_column(String(255), default="")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    recycled_count: Mapped[int] = mapped_column(Integer, default=0)
    retry_window: Mapped[str] = mapped_column(String(100), default="Next available window")
    default_hopper_queue: Mapped[str] = mapped_column(String(100), default="Auto Route to Hopper")
    tags: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class HopperEntry(Base):
    """Module: Live Calling Hopper entries awaiting agent / automated dialing."""

    __tablename__ = "hopper_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True)
    lead_id: Mapped[str] = mapped_column(String(36), ForeignKey("leads.id", ondelete="CASCADE"), index=True)
    recycle_list_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("recycle_lists.id", ondelete="SET NULL"), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True)
    campaign_name: Mapped[str] = mapped_column(String(255), default="Product")
    queue_name: Mapped[str] = mapped_column(String(100), default="Callback")  # Callback, Priority, General, Auto Route
    queue_badge_cls: Mapped[str] = mapped_column(String(20), default="b-i")
    priority: Mapped[str] = mapped_column(String(20), default="High")  # Urgent, High, Medium, Low
    priority_cls: Mapped[str] = mapped_column(String(20), default="red")  # red, orange, green, blue
    retry_count: Mapped[int] = mapped_column(Integer, default=1)
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    assigned_agent_name: Mapped[str] = mapped_column(String(255), default="Auto Route")
    status: Mapped[str] = mapped_column(String(50), default="Ready")  # Ready, Dialing, Waiting, Completed, Parked
    status_badge_cls: Mapped[str] = mapped_column(String(20), default="b-g")
    entered_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    dialed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# ---------------------------------------------------------------------------
# CallZenza AI Chatbot & RAG Models
# ---------------------------------------------------------------------------

class KnowledgeDocument(Base):
    """Uploaded or manually added documents for the CallZenza RAG knowledge base."""

    __tablename__ = "knowledge_documents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_key: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(String(50), default="v1", nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    department: Mapped[str | None] = mapped_column(String(100), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), default="")
    file_path: Mapped[str] = mapped_column(String(500), default="")
    file_type: Mapped[str] = mapped_column(String(50), default="TXT")  # PDF, DOCX, TXT, CSV, MANUAL, MD
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(50), default="UPLOADED", index=True)  # UPLOADED, PROCESSING, COMPLETED, FAILED
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by_user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class KnowledgeChunk(Base):
    """Text chunks and semantic embeddings extracted from KnowledgeDocuments."""

    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    document_id: Mapped[str] = mapped_column(String(36), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    agent_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    rag_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    agent_key: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    version_id: Mapped[str | None] = mapped_column(String(50), default="v1", nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON-serialized float array
    meta_info: Mapped[dict] = mapped_column(JSON, default=dict)  # page, section, source filename, etc.
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChatConversation(Base):
    """Persistent chat conversation sessions for CallZenza users and agents."""

    __tablename__ = "chat_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="New Conversation")
    is_global: Mapped[bool] = mapped_column(Boolean, default=False)
    pdf_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ChatMessage(Base):
    """Individual messages in a ChatConversation with user questions, AI answers, and RAG context."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(String(36), ForeignKey("chat_conversations.id", ondelete="CASCADE"), index=True)
    sender: Mapped[str] = mapped_column(String(20), default="user")  # 'user' or 'assistant'
    content: Mapped[str] = mapped_column(Text, default="")
    retrieved_context: Mapped[list] = mapped_column(JSON, default=list)  # snippets used for RAG
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ChatbotConfig(Base):
    """Configuration settings for the CallZenza AI Chatbot & RAG engine."""

    __tablename__ = "chatbot_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    model_name: Mapped[str] = mapped_column(String(100), default="gemini-3.6-flash")
    temperature: Mapped[float] = mapped_column(Float, default=0.7)
    top_k: Mapped[int] = mapped_column(Integer, default=5)
    similarity_threshold: Mapped[float] = mapped_column(Float, default=0.25)
    system_prompt: Mapped[str] = mapped_column(
        Text, 
        default=(
            "You are CallZenza AI Assistant — a universal, general-purpose multilingual intelligent assistant for CallZenza project knowledge support and general AI assistance.\n\n"
            "Rules for responding:\n"
            "1. UNIVERSAL MULTILINGUAL & TRANSLITERATION UNDERSTANDING:\n"
            "   - Understand and respond in any language used or requested by the user, including English, Tamil, Hindi, Telugu, Malayalam, Kannada, Bengali, Marathi, Gujarati, Punjabi, Urdu, Odia, Assamese, Arabic, Spanish, French, German, Portuguese, Japanese, Korean, Chinese, and any other supported language.\n"
            "   - Recognize transliterated and Romanized languages written using English characters (e.g. Tanglish: 'epdi iruka?', 'campaign create pannumbodhu agent select panna mudiyuma?', Hinglish: 'kaise ho?', 'ye kaise work karta hai?', Telugu: 'ela unnaru?', Malayalam: 'sukhamano?').\n"
            "   - Understand mixed-language sentences, informal/regional variations, and speech-to-text spelling variations (e.g. 'epdi/eppadi/yeppadi', 'pannu/panna/panradhu/pandrathu/pannunga') based on conversational context.\n\n"
            "2. LANGUAGE-MATCHED RESPONSE & DYNAMIC LANGUAGE SWITCHING:\n"
            "   - By default, respond in the same language, script, and conversational format used by the user (e.g. Tamil script for Tamil, Tanglish/Romanized Tamil for Tanglish, Hindi for Hindi, Hinglish for Hinglish, English for English, etc.).\n"
            "   - If the user explicitly requests another language (e.g., 'Translate this to Tamil', 'Idha English la sollu', 'Explain in Hindi'), follow the requested target language.\n"
            "   - If the user changes language during the conversation, dynamically switch the response language while preserving full conversation context.\n\n"
            "3. KNOWLEDGE ROUTING & DUAL-MODE HANDLING:\n"
            "   - For CallZenza Project Questions (about CallZenza features, uploaded scripts, agents, campaigns, leads, Lead Hopper, Lead Recycle, Admin/User Dashboards, calls, call transfers, RAG, chatbot, or platform workflows): Use retrieved CallZenza project context and verified data as your primary source of truth. Accurately describe actual implementations. Never invent fake project features, APIs, database fields, or workflows. If a specific CallZenza project detail is missing, state clearly that it is not available in the CallZenza knowledge base.\n"
            "   - For General Questions (about programming, technology, mathematics, science, business, education, general knowledge, coding, writing, translation, productivity, etc.): Use your comprehensive general LLM knowledge to provide direct, helpful, and thorough answers. Do NOT reject general questions or state that the question is outside scope or missing from knowledge base.\n"
            "   - For Hybrid Questions (combining general concepts with CallZenza implementation, e.g. 'RAG epdi work aagum and CallZenza la adha epdi use pannirukom?'): Combine general LLM knowledge with verified CallZenza project context into a single clear answer in the user's language.\n\n"
            "4. CONVERSATION CONTEXT & STYLE:\n"
            "   - Maintain multi-turn conversation context across text and voice.\n"
            "   - Provide natural, clear, well-structured responses formatted using clean markdown without prefixes such as 'According to CallZenza Knowledge Base:' or 'Answer:'."
        )
    )
    fallback_message: Mapped[str] = mapped_column(
        Text, 
        default="I don't have enough information in the CallZenza knowledge base to answer that specific project question."
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RAGConfig(Base):
    """Configuration settings for Enterprise RAG Knowledge Bases."""

    __tablename__ = "rag_configs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="Default Enterprise RAG")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE", index=True)  # ACTIVE, INACTIVE, ARCHIVED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RAGAgent(Base):
    """Department and Custom AI Agents with scoped RAG libraries."""

    __tablename__ = "rag_agents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    agent_key: Mapped[int] = mapped_column(Integer, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    archetype_id: Mapped[str] = mapped_column(String(50), default="custom")
    archetype_title: Mapped[str] = mapped_column(String(100), default="")
    archetype_desc: Mapped[str] = mapped_column(String(255), default="")
    archetype_icon: Mapped[str] = mapped_column(String(20), default="🤖")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    system_prompt: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class RAGCache(Base):
    """Auto-learned Q&A cache entries for RAG knowledge bases."""

    __tablename__ = "rag_caches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_key: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(50), default="gemini")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class RAGMiss(Base):
    """Tracks questions where RAG knowledge base lacked context (gap analysis)."""

    __tablename__ = "rag_misses"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    agent_key: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    question: Mapped[str] = mapped_column(String(500), nullable=False)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---------------------------------------------------------------------------
# Inbound Calls & User Groups Models
# ---------------------------------------------------------------------------

class UserGroup(Base):
    """User Groups module for organizing human agents and supervisors."""

    __tablename__ = "user_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)  # ACTIVE, INACTIVE
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class UserGroupMember(Base):
    """Many-to-many relationship mapping users to user groups."""

    __tablename__ = "user_group_members"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    group_id: Mapped[str] = mapped_column(String(36), ForeignKey("user_groups.id", ondelete="CASCADE"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    role_in_group: Mapped[str] = mapped_column(String(50), default="MEMBER")  # MEMBER, LEADER
    added_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class InboundNumber(Base):
    """Configured phone numbers for inbound call routing."""

    __tablename__ = "inbound_numbers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True, nullable=False)
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)  # ACTIVE, INACTIVE
    routing_type: Mapped[str] = mapped_column(String(50), default="VOICE_AGENT")  # VOICE_AGENT, HUMAN_AGENT, USER_GROUP, QUEUE, IVR
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_ai_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True, index=True)
    working_hours: Mapped[dict] = mapped_column(JSON, default=dict)
    fallback_action: Mapped[str] = mapped_column(String(50), default="VOICEMAIL")  # VOICEMAIL, AI_AGENT, HANGUP
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CallDisposition(Base):
    """Admin-configurable Call Dispositions and System Statuses."""

    __tablename__ = "call_dispositions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("businesses.id", ondelete="CASCADE"), index=True, nullable=True
    )
    status_code: Mapped[str] = mapped_column(String(30), index=True, nullable=False)
    status_name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(50), default="GENERAL")  # POSITIVE, NEGATIVE, CALLBACK, SYSTEM, GENERAL
    is_selectable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    color: Mapped[str | None] = mapped_column(String(30), default="indigo")  # emerald, rose, amber, indigo, slate
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class EmailLog(Base):
    """Model tracking sent emails, delivery IDs and statuses via Resend."""

    __tablename__ = "email_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True
    )
    to_email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    subject: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(50), default="pending")  # sent, failed, delivered, pending
    resend_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


# ---------------------------------------------------------------------------
# SIP Carrier, Trunk, DID Inventory, and Routing Models
# ---------------------------------------------------------------------------

class SIPCarrier(Base):
    __tablename__ = "sip_carriers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(50), default="ASTERISK")  # ASTERISK, TWILIO, FREEPBX, GENERIC_SIP, TATA, AIRTEL, JIO
    sip_host: Mapped[str] = mapped_column(String(255), nullable=False)
    sip_port: Mapped[int] = mapped_column(Integer, default=5060)
    transport: Mapped[str] = mapped_column(String(20), default="UDP")  # UDP, TCP, TLS
    auth_type: Mapped[str] = mapped_column(String(30), default="CREDENTIAL")  # IP_BASED, CREDENTIAL
    username: Mapped[str | None] = mapped_column(String(150), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    inbound_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    outbound_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    max_channels: Mapped[int] = mapped_column(Integer, default=30)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")  # ACTIVE, INACTIVE, MAINTENANCE
    last_health_status: Mapped[str] = mapped_column(String(50), default="UNKNOWN")  # HEALTHY, DEGRADED, UNREACHABLE, UNKNOWN
    last_health_latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_health_checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class SIPTrunk(Base):
    __tablename__ = "sip_trunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    carrier_id: Mapped[str] = mapped_column(String(36), ForeignKey("sip_carriers.id", ondelete="CASCADE"), index=True)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    direction: Mapped[str] = mapped_column(String(30), default="BIDIRECTIONAL")  # INBOUND, OUTBOUND, BIDIRECTIONAL
    max_concurrency: Mapped[int] = mapped_column(Integer, default=10)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    proxy_server: Mapped[str | None] = mapped_column(String(255), nullable=True)
    register_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    registration_status: Mapped[str] = mapped_column(String(50), default="PEER_ACTIVE")  # REGISTERED, UNREGISTERED, PEER_ACTIVE, FAILED
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")  # ACTIVE, DISABLED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


SipTrunk = SIPTrunk


class DIDNumber(Base):
    __tablename__ = "did_inventory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    sip_account_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    carrier_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_carriers.id", ondelete="SET NULL"), nullable=True, index=True)
    trunk_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_trunks.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    phone_number: Mapped[str] = mapped_column(String(60), nullable=False, index=True)

    sip_account: Mapped[Optional["SIPAccount"]] = relationship("SIPAccount", foreign_keys=[sip_account_id], back_populates="dids", lazy="selectin")
    name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    routing_type: Mapped[str] = mapped_column(String(50), default="AI_AGENT")  # AI_AGENT, HUMAN_AGENT, USER_GROUP, IVR, UNASSIGNED
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("ai_agents.id", ondelete="SET NULL"), nullable=True)
    assigned_voice_agent_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("experts.id", ondelete="SET NULL"), nullable=True)
    assigned_group_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("user_groups.id", ondelete="SET NULL"), nullable=True)
    is_outbound_caller_id: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(String(50), default="ACTIVE")  # ACTIVE, UNASSIGNED, SUSPENDED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CarrierRoutingPolicy(Base):
    __tablename__ = "carrier_routing_policies"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=True, index=True)
    primary_carrier_id: Mapped[str] = mapped_column(String(36), ForeignKey("sip_carriers.id", ondelete="CASCADE"))
    secondary_carrier_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("sip_carriers.id", ondelete="SET NULL"), nullable=True)
    max_retry_attempts: Mapped[int] = mapped_column(Integer, default=2)
    retry_on_status_codes: Mapped[dict] = mapped_column(JSON, default=lambda: ["503", "486", "408", "TIMEOUT", "CONGESTION"])
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class CarrierHealthLog(Base):
    __tablename__ = "carrier_health_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    carrier_id: Mapped[str] = mapped_column(String(36), ForeignKey("sip_carriers.id", ondelete="CASCADE"), index=True)
    check_type: Mapped[str] = mapped_column(String(50), default="SIP_OPTIONS")  # SIP_OPTIONS, SIP_REGISTER, SOCKET_PING
    status: Mapped[str] = mapped_column(String(50), nullable=False)  # HEALTHY, DEGRADED, UNREACHABLE
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    sip_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(150), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)


class DoNotCallRecord(Base):
    """Compliance & DND/DNC suppression record scoped per organization."""

    __tablename__ = "dnd_registry"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    business_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("businesses.id", ondelete="CASCADE"), nullable=True, index=True)
    phone_number: Mapped[str] = mapped_column(String(50), nullable=False)
    normalized_phone: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    lead_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("leads.id", ondelete="SET NULL"), nullable=True, index=True)
    campaign_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("campaigns.id", ondelete="SET NULL"), nullable=True, index=True)
    call_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(50), default="AI_VOICE_CALL")  # AI_VOICE_CALL, MANUAL, API, IMPORT
    reason: Mapped[str] = mapped_column(String(255), default="Customer requested Do Not Call")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)

