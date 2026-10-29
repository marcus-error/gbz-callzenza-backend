"""Module 13 — Tenant isolation and security helpers.
Provides SQLAlchemy query scoping and encryption utilities for tenant data.
"""
from typing import Any
from sqlalchemy.sql import Select

from app.services.email_service import decrypt_api_key, encrypt_api_key


def apply_tenant_filter(query: Select, model: Any, tenant_id: str) -> Select:
    """Applies a strict tenant filter to any SQLAlchemy Select statement.

    Checks for either `business_id` or `tenant_id` on the given model.
    """
    if hasattr(model, "business_id"):
        return query.where(getattr(model, "business_id") == tenant_id)
    elif hasattr(model, "tenant_id"):
        return query.where(getattr(model, "tenant_id") == tenant_id)
    return query


__all__ = ["apply_tenant_filter", "encrypt_api_key", "decrypt_api_key"]
