"""Module 5 — RBAC Permission re-exports and standard permission catalogue."""
from app.models.role import Permission, Role, role_permissions

STANDARD_PERMISSIONS = [
    # Tenant & Org
    ("tenant:read", "View tenant details", "TENANT"),
    ("tenant:write", "Update tenant configuration", "TENANT"),
    ("org:read", "View organization hierarchy", "ORGANIZATION"),
    ("org:write", "Create and modify organization units", "ORGANIZATION"),
    # Users & Roles
    ("users:read", "View users list and profiles", "USERS"),
    ("users:write", "Invite, update, and manage users", "USERS"),
    ("roles:read", "View roles and permissions", "ROLES"),
    ("roles:write", "Manage roles and assignments", "ROLES"),
    # Campaigns & Leads
    ("campaigns:read", "View campaigns and progress", "CAMPAIGNS"),
    ("campaigns:write", "Create and control campaigns", "CAMPAIGNS"),
    ("leads:read", "View leads and contact details", "LEADS"),
    ("leads:write", "Import and modify leads", "LEADS"),
    # Voice Agents & Calls
    ("agents:read", "View AI and voice agents", "AGENTS"),
    ("agents:write", "Configure and deploy voice agents", "AGENTS"),
    ("calls:read", "View call logs and recordings", "CALLS"),
    ("calls:barge", "Barge in and whisper on live calls", "CALLS"),
    # Reports & Analytics
    ("reports:read", "View reports and analytics", "REPORTS"),
    ("reports:export", "Export reports as CSV or PDF", "REPORTS"),
    # Billing & Settings
    ("billing:read", "View billing and subscription info", "BILLING"),
    ("billing:write", "Manage subscription and payment methods", "BILLING"),
    ("settings:read", "View tenant settings", "SETTINGS"),
    ("settings:write", "Update tenant settings and branding", "SETTINGS"),
    ("audit:read", "View audit trail logs", "AUDIT"),
    ("files:read", "View and download stored files", "FILES"),
    ("files:write", "Upload and delete files", "FILES"),
]

__all__ = ["Permission", "Role", "role_permissions", "STANDARD_PERMISSIONS"]
