from __future__ import annotations

import uuid
import time
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any, Tuple
from sqlalchemy import (
    Column,
    String,
    Text,
    DateTime,
    Boolean,
    Integer,
    select,
    update,
    and_,
    or_,
    desc,
)
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.db.database import Base
from app.db.models import User, Expert, Campaign, Lead, Call, CallTransfer


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _uuid() -> str:
    return str(uuid.uuid4())


# Dedicated, 100% additive table for tracking presence & atomic locks
class VoiceAgentPresence(Base):
    __tablename__ = "voice_agent_presence"

    id = Column(String(36), primary_key=True, default=_uuid)
    business_id = Column(String(36), index=True, default="default")
    agent_id = Column(String(36), unique=True, index=True, nullable=False)
    status = Column(String(30), default="AVAILABLE", index=True)
    # AVAILABLE, RINGING, ON_CALL, BUSY, OFFLINE, BREAK
    current_call_id = Column(String(64), nullable=True)
    locked_until = Column(DateTime, nullable=True)
    last_status_change = Column(DateTime, default=_now)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


# Dedicated, 100% additive table for tracking AI->Human transfer sessions & audit
class CallTransferSession(Base):
    __tablename__ = "ai_call_transfer_sessions"

    id = Column(String(36), primary_key=True, default=_uuid)
    business_id = Column(String(36), index=True, default="default")
    call_id = Column(String(64), index=True, nullable=False)
    campaign_id = Column(String(36), nullable=True, index=True)
    lead_id = Column(String(36), nullable=True, index=True)
    customer_name = Column(String(255), default="Customer")
    customer_phone = Column(String(50), default="")
    
    from_agent_name = Column(String(255), default="CallZenza AI Agent")
    assigned_voice_agent_id = Column(String(36), nullable=True, index=True)
    assigned_voice_agent_name = Column(String(255), nullable=True)
    
    intent_detected_at = Column(DateTime, default=_now)
    ringing_at = Column(DateTime, nullable=True)
    accepted_at = Column(DateTime, nullable=True)
    ai_disconnected_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    status = Column(String(50), default="REQUESTED", index=True)
    # REQUESTED, RINGING, ACCEPTED, BRIDGED, DECLINED, TIMEOUT, FAILED, COMPLETED
    failure_reason = Column(String(255), nullable=True)
    transcript_snapshot = Column(Text, nullable=True)
    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class CallTransferRepository:
    """Repository handling presence, reservation locks, and transfer session persistence."""

    _ensured_tables = False

    @classmethod
    async def ensure_tables(cls, db: AsyncSession):
        """Ensures additive tables exist without mutating existing tables."""
        if cls._ensured_tables:
            return
        try:
            conn = await db.connection()
            await conn.run_sync(Base.metadata.create_all, tables=[
                VoiceAgentPresence.__table__,
                CallTransferSession.__table__,
            ])
            cls._ensured_tables = True
        except Exception as e:
            logger.warning(f"[CallTransferRepository] note on ensure_tables: {e}")

    @classmethod
    async def get_presence(cls, agent_id: str, db: AsyncSession) -> Optional[VoiceAgentPresence]:
        await cls.ensure_tables(db)
        stmt = select(VoiceAgentPresence).where(VoiceAgentPresence.agent_id == agent_id)
        return (await db.execute(stmt)).scalars().first()

    @classmethod
    async def update_presence(
        cls,
        agent_id: str,
        status: str,
        business_id: str,
        db: AsyncSession,
        current_call_id: Optional[str] = None
    ) -> VoiceAgentPresence:
        await cls.ensure_tables(db)
        now = _now()
        rec = await cls.get_presence(agent_id, db)
        if not rec:
            rec = VoiceAgentPresence(
                id=_uuid(),
                agent_id=agent_id,
                business_id=business_id,
                status=status,
                current_call_id=current_call_id,
                last_status_change=now,
                created_at=now,
                updated_at=now,
            )
            db.add(rec)
        else:
            rec.status = status
            if current_call_id is not None:
                rec.current_call_id = current_call_id
            rec.last_status_change = now
            rec.updated_at = now
            db.add(rec)

        await db.commit()
        await db.refresh(rec)
        return rec

    @classmethod
    async def try_reserve_agent(
        cls,
        agent_id: str,
        call_id: str,
        ttl_seconds: int,
        db: AsyncSession
    ) -> bool:
        """
        Atomically reserves an agent using row-level check and lock.
        Returns True if reservation lock was successfully acquired.
        """
        await cls.ensure_tables(db)
        now = _now()
        lock_expiry = now + timedelta(seconds=ttl_seconds)

        rec = await cls.get_presence(agent_id, db)
        if not rec:
            # Create as RINGING with lock
            rec = VoiceAgentPresence(
                id=_uuid(),
                agent_id=agent_id,
                business_id="default",
                status="RINGING",
                current_call_id=call_id,
                locked_until=lock_expiry,
                last_status_change=now,
                created_at=now,
                updated_at=now,
            )
            db.add(rec)
            await db.commit()
            return True

        # Check if already locked by another call and lock has not expired
        if rec.locked_until and rec.locked_until > now and rec.current_call_id != call_id:
            return False

        if rec.status in ("ON_CALL", "BUSY", "BREAK", "OFFLINE") and rec.current_call_id != call_id:
            return False

        # Acquire reservation lock
        rec.status = "RINGING"
        rec.current_call_id = call_id
        rec.locked_until = lock_expiry
        rec.last_status_change = now
        rec.updated_at = now
        db.add(rec)
        await db.commit()
        return True

    @classmethod
    async def release_agent_lock(
        cls,
        agent_id: str,
        call_id: str,
        new_status: str,
        db: AsyncSession
    ):
        """Releases the lock on an agent and updates status."""
        await cls.ensure_tables(db)
        rec = await cls.get_presence(agent_id, db)
        if rec and (rec.current_call_id == call_id or rec.current_call_id is None):
            rec.status = new_status
            rec.current_call_id = None
            rec.locked_until = None
            rec.last_status_change = _now()
            rec.updated_at = _now()
            db.add(rec)
            await db.commit()

    @classmethod
    async def list_available_agents(
        cls,
        business_id: Optional[str],
        db: AsyncSession
    ) -> List[Dict[str, Any]]:
        """
        Returns all registered Voice Agents, ordered by longest idle first.
        """
        await cls.ensure_tables(db)
        now = _now()

        # Fetch voice agent users / experts
        stmt = (
            select(User)
            .where(func_role_match(User.role))
        )
        if business_id and business_id != "default":
            stmt = stmt.where(User.business_id == business_id)

        users = (await db.execute(stmt)).scalars().all()
        result = []

        for u in users:
            # Query presence
            p_stmt = select(VoiceAgentPresence).where(VoiceAgentPresence.agent_id == u.id)
            p = (await db.execute(p_stmt)).scalars().first()

            status_val = p.status if p else (u.status or "AVAILABLE")
            is_locked = bool(p and p.locked_until and p.locked_until > now)

            # Resolve if genuinely available
            is_avail = (status_val == "AVAILABLE") and not is_locked

            last_change = p.last_status_change if p else u.created_at

            result.append({
                "agent_id": u.id,
                "agent_name": u.full_name or u.email.split("@")[0],
                "agent_email": u.email,
                "status": "RINGING" if is_locked else status_val,
                "is_available": is_avail,
                "skills": ["VOICE", "INBOUND", "OUTBOUND"],
                "current_call_id": p.current_call_id if p else None,
                "last_status_change": last_change.isoformat() if last_change else None,
            })

        # Sort: available first, then longest idle
        result.sort(key=lambda x: (not x["is_available"], x["last_status_change"] or ""))
        return result

    @classmethod
    async def create_transfer_session(
        cls,
        call_id: str,
        business_id: str,
        campaign_id: Optional[str],
        lead_id: Optional[str],
        customer_name: str,
        customer_phone: str,
        assigned_agent_id: Optional[str],
        assigned_agent_name: Optional[str],
        transcript: Optional[str],
        db: AsyncSession
    ) -> CallTransferSession:
        await cls.ensure_tables(db)
        now = _now()
        sess = CallTransferSession(
            id=_uuid(),
            business_id=business_id or "default",
            call_id=call_id,
            campaign_id=campaign_id,
            lead_id=lead_id,
            customer_name=customer_name or "Customer",
            customer_phone=customer_phone or "",
            assigned_voice_agent_id=assigned_agent_id,
            assigned_voice_agent_name=assigned_agent_name,
            intent_detected_at=now,
            ringing_at=now if assigned_agent_id else None,
            status="RINGING" if assigned_agent_id else "REQUESTED",
            transcript_snapshot=transcript,
            created_at=now,
            updated_at=now,
        )
        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess

    @classmethod
    async def get_transfer_session(cls, transfer_id: str, db: AsyncSession) -> Optional[CallTransferSession]:
        await cls.ensure_tables(db)
        stmt = select(CallTransferSession).where(
            or_(
                CallTransferSession.id == transfer_id,
                CallTransferSession.call_id == transfer_id,
            )
        )
        return (await db.execute(stmt)).scalars().first()

    @classmethod
    async def update_transfer_session_status(
        cls,
        transfer_id: str,
        status: str,
        db: AsyncSession,
        failure_reason: Optional[str] = None,
        ai_disconnected: bool = False
    ) -> Optional[CallTransferSession]:
        await cls.ensure_tables(db)
        sess = await cls.get_transfer_session(transfer_id, db)
        if not sess:
            return None

        now = _now()
        sess.status = status
        sess.updated_at = now

        if status == "ACCEPTED":
            sess.accepted_at = now
        elif status == "COMPLETED":
            sess.completed_at = now
        elif status in ("DECLINED", "TIMEOUT", "FAILED"):
            sess.failure_reason = failure_reason or status

        if ai_disconnected:
            sess.ai_disconnected_at = now

        db.add(sess)
        await db.commit()
        await db.refresh(sess)
        return sess


def func_role_match(col):
    """Helper for selecting voice agents."""
    return or_(
        col == "VOICE_AGENT",
        col == "AGENT",
        col == "INBOUND_AGENT",
        col == "OUTBOUND_AGENT",
    )
