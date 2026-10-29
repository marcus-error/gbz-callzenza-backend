from __future__ import annotations

from typing import Optional, List, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from app.repositories.call_transfer_repository import CallTransferRepository, VoiceAgentPresence
from app.db.models import User, Expert


DEFAULT_RESERVATION_TTL_SECONDS = 20


class AgentAvailabilityService:
    """Manages voice agent presence, atomic reservation locking, and matching strategy."""

    @classmethod
    async def get_presence(cls, agent_id: str, db: AsyncSession) -> Optional[Dict[str, Any]]:
        presence = await CallTransferRepository.get_presence(agent_id, db)
        if not presence:
            user = await db.get(User, agent_id)
            if not user:
                return None
            return {
                "agent_id": user.id,
                "agent_name": user.full_name or user.email,
                "status": user.status or "AVAILABLE",
                "is_available": (user.status or "AVAILABLE") == "AVAILABLE",
            }
        return {
            "agent_id": presence.agent_id,
            "status": presence.status,
            "is_available": presence.status == "AVAILABLE",
            "current_call_id": presence.current_call_id,
            "last_status_change": presence.last_status_change.isoformat() if presence.last_status_change else None,
        }

    @classmethod
    async def set_presence(
        cls,
        agent_id: str,
        status: str,
        business_id: str,
        db: AsyncSession
    ) -> Dict[str, Any]:
        """Sets presence status: AVAILABLE, RINGING, ON_CALL, BUSY, OFFLINE, BREAK."""
        valid_statuses = {"AVAILABLE", "RINGING", "ON_CALL", "BUSY", "OFFLINE", "BREAK"}
        clean_status = status.upper().strip()
        if clean_status not in valid_statuses:
            clean_status = "AVAILABLE"

        res = await CallTransferRepository.update_presence(
            agent_id=agent_id,
            status=clean_status,
            business_id=business_id,
            db=db
        )
        return {
            "agent_id": res.agent_id,
            "status": res.status,
            "is_available": res.status == "AVAILABLE",
            "last_status_change": res.last_status_change.isoformat() if res.last_status_change else None,
        }

    @classmethod
    async def list_agents_presence(
        cls,
        business_id: Optional[str],
        db: AsyncSession
    ) -> Dict[str, Any]:
        agents = await CallTransferRepository.list_available_agents(business_id, db)
        available_count = sum(1 for a in agents if a["is_available"])
        return {
            "agents": agents,
            "total_available": available_count,
            "total_agents": len(agents),
        }

    @classmethod
    async def find_and_reserve_available_agent(
        cls,
        call_id: str,
        business_id: Optional[str],
        db: AsyncSession,
        exclude_agent_ids: Optional[List[str]] = None,
        preferred_agent_id: Optional[str] = None,
        ttl_seconds: int = DEFAULT_RESERVATION_TTL_SECONDS,
    ) -> Optional[Dict[str, Any]]:
        """
        Picks the best available agent (longest idle first) and atomically acquires a lock.
        If preferred_agent_id is provided and available, tries that agent first.
        """
        exclude_set = set(exclude_agent_ids or [])
        agents = await CallTransferRepository.list_available_agents(business_id, db)

        # 1. Check preferred agent first if requested (resolving Expert to User ID if needed)
        if preferred_agent_id and preferred_agent_id not in exclude_set:
            resolved_pref_id = preferred_agent_id
            try:
                from app.db.models import Expert
                exp = await db.get(Expert, preferred_agent_id)
                if exp and exp.user_id:
                    resolved_pref_id = exp.user_id
            except Exception:
                pass

            pref = next((a for a in agents if a["agent_id"] in (preferred_agent_id, resolved_pref_id)), None)
            if pref and pref["is_available"]:
                target_lock_id = pref["agent_id"]
                locked = await CallTransferRepository.try_reserve_agent(
                    agent_id=target_lock_id,
                    call_id=call_id,
                    ttl_seconds=ttl_seconds,
                    db=db
                )
                if locked:
                    logger.info(f"[Availability] Reserved preferred agent {target_lock_id} for call {call_id}")
                    return pref

        # 2. Strategy: Longest idle first
        for candidate in agents:
            c_id = candidate["agent_id"]
            if c_id in exclude_set or not candidate["is_available"]:
                continue

            locked = await CallTransferRepository.try_reserve_agent(
                agent_id=c_id,
                call_id=call_id,
                ttl_seconds=ttl_seconds,
                db=db
            )
            if locked:
                logger.info(f"[Availability] Reserved agent {c_id} ({candidate['agent_name']}) for call {call_id}")
                return candidate

        logger.warning(f"[Availability] No available Voice Agent could be reserved for call {call_id}")
        return None

    @classmethod
    async def release_reservation(
        cls,
        agent_id: str,
        call_id: str,
        db: AsyncSession,
        new_status: str = "AVAILABLE"
    ):
        """Releases the lock on an agent."""
        await CallTransferRepository.release_agent_lock(
            agent_id=agent_id,
            call_id=call_id,
            new_status=new_status,
            db=db
        )
        logger.info(f"[Availability] Released reservation for agent {agent_id} back to {new_status}")

    @classmethod
    async def mark_agent_on_call(
        cls,
        agent_id: str,
        call_id: str,
        business_id: str,
        db: AsyncSession
    ):
        await CallTransferRepository.update_presence(
            agent_id=agent_id,
            status="ON_CALL",
            business_id=business_id,
            db=db,
            current_call_id=call_id
        )
