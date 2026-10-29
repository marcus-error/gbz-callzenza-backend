from datetime import datetime, timezone
from typing import Any, Dict, Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import AsyncSessionLocal
from app.db.models import AuditLog


async def log_action(
    action: str,
    entity: str,
    tenant_id: str,
    actor: Optional[str] = None,
    entity_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
    db: Optional[AsyncSession] = None,
) -> Optional[AuditLog]:
    """Helper to record an audit log event scoped to tenant."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    async def _save(session: AsyncSession) -> AuditLog:
        record = AuditLog(
            business_id=tenant_id,
            user_id=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            details=metadata or {},
            created_at=now,
        )
        session.add(record)
        await session.commit()
        await session.refresh(record)
        return record

    try:
        if db:
            return await _save(db)
        else:
            async with AsyncSessionLocal() as session:
                return await _save(session)
    except Exception as exc:
        logger.error(f"[AUDIT LOG ERROR] Failed to record audit log for action={action}: {exc}")
        return None
