"""Call Recording Management: Add campaign association, 24h expiry, and Google Drive backup columns.

Revision ID: 20261013_call_recording_management
Revises: 20261009_agent_portal_access
Create Date: 2026-10-13 10:00:00.000000
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '20261013_call_recording_management'
down_revision: Union[str, Sequence[str], None] = '20261009_agent_portal_access'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Ensure call_recordings table exists
    try:
        op.create_table(
            'call_recordings',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('call_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('segment_index', sa.Integer(), server_default='0', nullable=False),
            sa.Column('source', sa.String(length=50), server_default='provider'),
            sa.Column('storage_provider', sa.String(length=50), server_default='local'),
            sa.Column('storage_key', sa.String(length=500), nullable=False),
            sa.Column('format', sa.String(length=20), server_default='wav'),
            sa.Column('channels', sa.Integer(), server_default='1'),
            sa.Column('duration_ms', sa.Integer(), nullable=True),
            sa.Column('size_bytes', sa.Integer(), nullable=True),
            sa.Column('checksum', sa.String(length=64), nullable=True),
            sa.Column('status', sa.String(length=30), server_default='ready'),
            sa.Column('tenant_id', sa.String(length=36), nullable=True, index=True),
            sa.Column('campaign_id', sa.String(length=36), nullable=True, index=True),
            sa.Column('call_direction', sa.String(length=20), server_default='OUTBOUND'),
            sa.Column('expires_at', sa.DateTime(), nullable=True, index=True),
            sa.Column('recording_status', sa.String(length=30), server_default='available', index=True),
            sa.Column('backup_status', sa.String(length=30), server_default='NOT_BACKED_UP', index=True),
            sa.Column('google_drive_file_id', sa.String(length=255), nullable=True),
            sa.Column('google_drive_folder_id', sa.String(length=255), nullable=True),
            sa.Column('google_drive_file_url', sa.String(length=500), nullable=True),
            sa.Column('backup_completed_at', sa.DateTime(), nullable=True),
            sa.Column('backup_error', sa.String(length=500), nullable=True),
            sa.Column('cleanup_status', sa.String(length=30), server_default='pending'),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
    except Exception:
        # Table already exists; add columns additively
        columns = [
            ('tenant_id', sa.String(length=36)),
            ('campaign_id', sa.String(length=36)),
            ('call_direction', sa.String(length=20)),
            ('expires_at', sa.DateTime()),
            ('recording_status', sa.String(length=30)),
            ('backup_status', sa.String(length=30)),
            ('google_drive_file_id', sa.String(length=255)),
            ('google_drive_folder_id', sa.String(length=255)),
            ('google_drive_file_url', sa.String(length=500)),
            ('backup_completed_at', sa.DateTime()),
            ('backup_error', sa.String(length=500)),
            ('cleanup_status', sa.String(length=30)),
            ('updated_at', sa.DateTime()),
        ]
        for col_name, col_type in columns:
            try:
                op.add_column('call_recordings', sa.Column(col_name, col_type, nullable=True))
            except Exception:
                pass

    # 2. Add composite indexes if supported
    try:
        op.create_index('ix_call_recordings_campaign_created', 'call_recordings', ['campaign_id', 'created_at'])
    except Exception:
        pass
    try:
        op.create_index('ix_call_recordings_tenant_campaign', 'call_recordings', ['tenant_id', 'campaign_id'])
    except Exception:
        pass
    try:
        op.create_index('ix_call_recordings_expiry_status', 'call_recordings', ['expires_at', 'recording_status'])
    except Exception:
        pass


def downgrade() -> None:
    pass
