"""Multi-Tenant Subdomain & Cluster Routing Middleware.

Resolves tenant context per-request:
1. Detects subdomain from Host header (e.g. acme.callzenza.com, acme.localhost:3000),
   or from headers 'X-Tenant-Subdomain', 'X-Tenant-Slug', or query param 'tenant_subdomain'.
2. Attaches tenant info (id, slug, subdomain, cluster_id) to request.state.
3. Sets informative response headers (X-Tenant-Subdomain, X-Tenant-Cluster).
4. Gracefully skips system subdomains (www, api, app) and works transparently with all existing routes.
"""
from __future__ import annotations

import re
import time
from typing import Dict, Optional, Tuple
from fastapi import Request
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import select

from app.core.config import get_settings
from app.db.database import async_session_factory
from app.models.tenant import Tenant
from app.db.models import Business

# System-reserved subdomains that do not represent specific tenant workspaces
SYSTEM_SUBDOMAINS = {"www", "api", "app", "admin", "mail", "cdn", "assets", "static", "staging", "dev"}

# In-memory tenant routing cache: (subdomain) -> (tenant_id, slug, cluster_id, server_url, isolation_strategy, expiry)
_ROUTING_CACHE: Dict[str, Tuple[str, str, str, Optional[str], str, float]] = {}
CACHE_TTL_SECONDS = 60.0


def extract_subdomain(host: str, base_domain: str = "callzenza.com") -> Optional[str]:
    """Extracts tenant subdomain from host string."""
    if not host:
        return None
    
    # Strip port if present
    hostname = host.split(":")[0].strip().lower()

    # Case 1: *.localhost (e.g. acme.localhost)
    if hostname.endswith(".localhost"):
        sub = hostname[:-10]
        if sub and sub not in SYSTEM_SUBDOMAINS and "." not in sub:
            return sub
        return None

    # Case 2: Standard custom base domain (e.g. acme.callzenza.com)
    clean_base = base_domain.split(":")[0].strip().lower()
    if hostname.endswith(f".{clean_base}"):
        sub = hostname[: -(len(clean_base) + 1)]
        if sub and sub not in SYSTEM_SUBDOMAINS and "." not in sub:
            return sub
        return None

    # Case 3: Generic domain with >= 3 parts (e.g. acme.example.com)
    parts = hostname.split(".")
    if len(parts) >= 3 and not re.match(r"^\d+\.\d+\.\d+\.\d+$", hostname):
        candidate = parts[0]
        if candidate not in SYSTEM_SUBDOMAINS:
            return candidate

    return None


async def resolve_tenant_by_subdomain(subdomain: str) -> Optional[dict]:
    """Resolves tenant details from database with in-memory caching."""
    now = time.time()
    clean_sub = subdomain.strip().lower()

    if clean_sub in _ROUTING_CACHE:
        tid, slug, cluster_id, server_url, isolation_strategy, exp = _ROUTING_CACHE[clean_sub]
        if now < exp:
            return {
                "id": tid,
                "slug": slug,
                "subdomain": clean_sub,
                "cluster_id": cluster_id,
                "server_url": server_url,
                "isolation_strategy": isolation_strategy,
            }

    # Query DB
    async with async_session_factory() as session:
        # Check Tenant table first
        stmt = select(Tenant).where(
            (Tenant.subdomain == clean_sub) | (Tenant.slug == clean_sub) | (Tenant.id == clean_sub)
        ).limit(1)
        res = await session.execute(stmt)
        tenant = res.scalar_one_or_none()

        if tenant:
            cluster_id = getattr(tenant, "cluster_id", "default") or "default"
            server_url = getattr(tenant, "server_url", None)
            isolation = getattr(tenant, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE"
            _ROUTING_CACHE[clean_sub] = (tenant.id, tenant.slug, cluster_id, server_url, isolation, now + CACHE_TTL_SECONDS)
            return {
                "id": tenant.id,
                "slug": tenant.slug,
                "subdomain": clean_sub,
                "cluster_id": cluster_id,
                "server_url": server_url,
                "isolation_strategy": isolation,
            }

        # Check Business table fallback
        b_stmt = select(Business).where(
            (Business.subdomain == clean_sub) | (Business.slug == clean_sub) | (Business.id == clean_sub)
        ).limit(1)
        b_res = await session.execute(b_stmt)
        biz = b_res.scalar_one_or_none()

        if biz:
            cluster_id = getattr(biz, "cluster_id", "default") or "default"
            server_url = getattr(biz, "server_url", None)
            isolation = getattr(biz, "isolation_strategy", "SHARED_DATABASE") or "SHARED_DATABASE"
            _ROUTING_CACHE[clean_sub] = (biz.id, biz.slug or clean_sub, cluster_id, server_url, isolation, now + CACHE_TTL_SECONDS)
            return {
                "id": biz.id,
                "slug": biz.slug or clean_sub,
                "subdomain": clean_sub,
                "cluster_id": cluster_id,
                "server_url": server_url,
                "isolation_strategy": isolation,
            }

    return None


class TenantSubdomainMiddleware(BaseHTTPMiddleware):
    """Intercepts requests to resolve tenant subdomain and cluster routing metadata."""

    async def dispatch(self, request: Request, call_next):
        settings = get_settings()
        host = request.headers.get("host", "")
        
        # 1. Resolve subdomain from explicit headers or query params, falling back to Host header
        subdomain = (
            request.headers.get("x-tenant-subdomain")
            or request.headers.get("x-tenant-slug")
            or request.headers.get("x-tenant-id")
            or request.query_params.get("tenant_subdomain")
            or request.query_params.get("tenant_id")
            or extract_subdomain(host, settings.base_domain)
        )

        tenant_data = None
        if subdomain:
            clean_sub = subdomain.strip().lower()
            try:
                tenant_data = await resolve_tenant_by_subdomain(clean_sub)
            except Exception as err:
                logger.debug(f"[SUBDOMAIN MIDDLEWARE] Error resolving tenant '{clean_sub}': {err}")

        # Attach to request state for downstream handlers and dependencies
        if tenant_data:
            request.state.tenant_id = tenant_data["id"]
            request.state.tenant_subdomain = tenant_data["subdomain"]
            request.state.tenant_slug = tenant_data["slug"]
            request.state.tenant_cluster_id = tenant_data["cluster_id"]
            request.state.tenant_server_url = tenant_data["server_url"]
            request.state.tenant_isolation_strategy = tenant_data["isolation_strategy"]
        else:
            request.state.tenant_id = None
            request.state.tenant_subdomain = None
            request.state.tenant_slug = None
            request.state.tenant_cluster_id = "default"
            request.state.tenant_server_url = None
            request.state.tenant_isolation_strategy = "SHARED_DATABASE"

        response = await call_next(request)

        # Inject diagnostic routing response headers
        if tenant_data:
            response.headers["X-Tenant-Id"] = tenant_data["id"]
            response.headers["X-Tenant-Subdomain"] = tenant_data["subdomain"]
            response.headers["X-Tenant-Cluster"] = tenant_data["cluster_id"]

        return response
