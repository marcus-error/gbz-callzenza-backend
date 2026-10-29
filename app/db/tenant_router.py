"""Dynamic Multi-Tenant Database Router & Manager (SaaS Data Isolation).

Supports three levels of data isolation:
1. SHARED_DATABASE (default): Standard row-level multi-tenancy using tenant_id / business_id filtering.
2. SEPARATE_SCHEMA: Isolated PostgreSQL schemas per tenant (e.g., search_path = tenant_<slug>).
3. SEPARATE_DATABASE: Completely isolated physical/logical database connection per tenant.
"""
from __future__ import annotations

import asyncio
from typing import AsyncGenerator, Dict, Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy import text

from app.db.database import engine as default_engine, async_session_factory as default_session_factory


class TenantDatabaseManager:
    """Manages dedicated database engines and session factories for isolated tenants."""

    def __init__(self) -> None:
        self._engines: Dict[str, AsyncEngine] = {}
        self._session_factories: Dict[str, async_sessionmaker[AsyncSession]] = {}
        self._lock = asyncio.Lock()

    def get_default_engine(self) -> AsyncEngine:
        return default_engine

    async def get_tenant_engine(
        self,
        tenant_id: str,
        isolation_strategy: str = "SHARED_DATABASE",
        database_url: Optional[str] = None,
        schema_name: Optional[str] = None,
    ) -> AsyncEngine:
        """Returns the appropriate AsyncEngine for a given tenant."""
        # Case 1: Standard shared database (row-level isolation)
        if isolation_strategy == "SHARED_DATABASE" or not database_url:
            return default_engine

        # Case 2: Dedicated isolated database
        cache_key = f"{tenant_id}:{database_url}"
        if cache_key in self._engines:
            return self._engines[cache_key]

        async with self._lock:
            if cache_key in self._engines:
                return self._engines[cache_key]

            logger.info(f"[TENANT DB] Initializing isolated database engine for tenant {tenant_id}")
            # Ensure proper asyncpg / aiosqlite driver prefix
            clean_url = database_url
            if clean_url.startswith("postgresql://"):
                clean_url = clean_url.replace("postgresql://", "postgresql+asyncpg://", 1)
            elif clean_url.startswith("sqlite://"):
                clean_url = clean_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

            try:
                isolated_engine = create_async_engine(
                    clean_url,
                    echo=False,
                    pool_pre_ping=True,
                    pool_recycle=1800,
                )
                self._engines[cache_key] = isolated_engine
                self._session_factories[cache_key] = async_sessionmaker(
                    isolated_engine,
                    class_=AsyncSession,
                    expire_on_commit=False,
                )
                return isolated_engine
            except Exception as e:
                logger.error(f"[TENANT DB] Failed to initialize isolated engine for {tenant_id}: {e}. Falling back to default.")
                return default_engine

    async def get_tenant_session(
        self,
        tenant_id: str,
        isolation_strategy: str = "SHARED_DATABASE",
        database_url: Optional[str] = None,
        schema_name: Optional[str] = None,
    ) -> AsyncGenerator[AsyncSession, None]:
        """Provides an AsyncSession routed to either the tenant's isolated DB or the shared default DB."""
        if isolation_strategy == "SEPARATE_DATABASE" and database_url:
            cache_key = f"{tenant_id}:{database_url}"
            if cache_key not in self._session_factories:
                await self.get_tenant_engine(tenant_id, isolation_strategy, database_url, schema_name)
            
            factory = self._session_factories.get(cache_key, default_session_factory)
            async with factory() as session:
                yield session
        elif isolation_strategy == "SEPARATE_SCHEMA" and schema_name and default_engine.name != "sqlite":
            async with default_session_factory() as session:
                await session.execute(text(f"SET search_path TO {schema_name}, public"))
                yield session
        else:
            async with default_session_factory() as session:
                yield session

    async def close_all(self) -> None:
        """Disposes all isolated tenant engines cleanly on shutdown."""
        async with self._lock:
            for key, eng in list(self._engines.items()):
                try:
                    await eng.dispose()
                except Exception as e:
                    logger.debug(f"[TENANT DB] Error closing engine {key}: {e}")
            self._engines.clear()
            self._session_factories.clear()


# Global Singleton instance
tenant_db_manager = TenantDatabaseManager()
