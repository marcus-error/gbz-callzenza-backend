"""Multi-tenant pure additive migration

Revision ID: 20260913_multitenant
Revises: ab800f274e53
Create Date: 2026-09-13 18:50:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '20260913_multitenant'
down_revision: Union[str, Sequence[str], None] = 'ab800f274e53'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create new tables
    op.create_table(
        'tenants',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('slug', sa.String(length=100), unique=True, index=True, nullable=False),
        sa.Column('status', sa.String(length=50), server_default='active', index=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'organizations',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), index=True, nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('org_type', sa.String(length=50), server_default='COMPANY', index=True),
        sa.Column('code', sa.String(length=50), nullable=True),
        sa.Column('parent_id', sa.String(length=36), nullable=True),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('details', sa.JSON(), nullable=True),
        sa.Column('status', sa.String(length=50), server_default='active', index=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'roles',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), index=True, nullable=True),
        sa.Column('name', sa.String(length=100), index=True, nullable=False),
        sa.Column('description', sa.String(length=255), server_default=''),
        sa.Column('is_system', sa.Boolean(), server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'permissions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=100), unique=True, index=True, nullable=False),
        sa.Column('description', sa.String(length=255), server_default=''),
        sa.Column('module', sa.String(length=50), server_default='GENERAL', index=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'role_permissions',
        sa.Column('role_id', sa.String(length=36), sa.ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('permission_id', sa.String(length=36), sa.ForeignKey('permissions.id', ondelete='CASCADE'), primary_key=True),
    )

    op.create_table(
        'tenant_brandings',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), unique=True, index=True, nullable=False),
        sa.Column('company_name', sa.String(length=255), server_default=''),
        sa.Column('logo_url', sa.String(length=500), server_default=''),
        sa.Column('dark_logo_url', sa.String(length=500), server_default=''),
        sa.Column('favicon_url', sa.String(length=500), server_default=''),
        sa.Column('primary_color', sa.String(length=30), server_default='#4F46E5'),
        sa.Column('secondary_color', sa.String(length=30), server_default='#06B6D4'),
        sa.Column('accent_color', sa.String(length=30), server_default='#10B981'),
        sa.Column('custom_css', sa.Text(), nullable=True),
        sa.Column('support_email', sa.String(length=255), server_default=''),
        sa.Column('support_phone', sa.String(length=50), server_default=''),
        sa.Column('website', sa.String(length=255), server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'plans',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('name', sa.String(length=100), unique=True, nullable=False),
        sa.Column('slug', sa.String(length=100), unique=True, index=True, nullable=False),
        sa.Column('description', sa.String(length=255), server_default=''),
        sa.Column('price_cents', sa.Integer(), server_default='0'),
        sa.Column('billing_interval', sa.String(length=20), server_default='monthly'),
        sa.Column('max_agents', sa.Integer(), server_default='5'),
        sa.Column('max_concurrent_calls', sa.Integer(), server_default='2'),
        sa.Column('max_leads', sa.Integer(), server_default='1000'),
        sa.Column('max_minutes_monthly', sa.Integer(), server_default='300'),
        sa.Column('features', sa.JSON(), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='1'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'subscriptions',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), index=True, nullable=False),
        sa.Column('plan_id', sa.String(length=36), sa.ForeignKey('plans.id'), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='ACTIVE', index=True),
        sa.Column('auto_renew', sa.Boolean(), server_default='1'),
        sa.Column('start_date', sa.DateTime(), nullable=True),
        sa.Column('end_date', sa.DateTime(), nullable=True),
        sa.Column('payment_method_id', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'notification_logs',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), index=True, nullable=False),
        sa.Column('channel', sa.String(length=20), index=True, nullable=False),
        sa.Column('recipient', sa.String(length=255), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('status', sa.String(length=50), server_default='QUEUED', index=True),
        sa.Column('external_id', sa.String(length=100), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('metadata', sa.JSON(), nullable=True),
        sa.Column('sent_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'tenant_settings',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), unique=True, index=True, nullable=False),
        sa.Column('timezone', sa.String(length=50), server_default='UTC'),
        sa.Column('currency', sa.String(length=10), server_default='USD'),
        sa.Column('date_format', sa.String(length=20), server_default='YYYY-MM-DD'),
        sa.Column('calling_hours_start', sa.String(length=10), server_default='09:00'),
        sa.Column('calling_hours_end', sa.String(length=10), server_default='18:00'),
        sa.Column('retry_limit', sa.Integer(), server_default='3'),
        sa.Column('retry_interval_minutes', sa.Integer(), server_default='60'),
        sa.Column('voicemail_detection', sa.Boolean(), server_default='1'),
        sa.Column('custom_flags', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'tenant_files',
        sa.Column('id', sa.String(length=36), primary_key=True),
        sa.Column('tenant_id', sa.String(length=36), index=True, nullable=False),
        sa.Column('filename', sa.String(length=255), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('file_size_bytes', sa.Integer(), server_default='0'),
        sa.Column('mime_type', sa.String(length=100), server_default='application/octet-stream'),
        sa.Column('file_path', sa.String(length=500), nullable=False),
        sa.Column('storage_type', sa.String(length=20), server_default='LOCAL'),
        sa.Column('uploader_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # 2. Add nullable tenant_id to existing scoped tables
    for tbl in ["users", "campaigns", "leads", "calls", "lead_groups", "experts", "email_logs"]:
        try:
            with op.batch_alter_table(tbl) as batch_op:
                batch_op.add_column(sa.Column('tenant_id', sa.String(length=36), nullable=True))
        except Exception:
            pass


def downgrade() -> None:
    for tbl in ["tenant_files", "tenant_settings", "notification_logs", "subscriptions", "plans",
                "tenant_brandings", "role_permissions", "permissions", "roles", "organizations", "tenants"]:
        op.drop_table(tbl)
