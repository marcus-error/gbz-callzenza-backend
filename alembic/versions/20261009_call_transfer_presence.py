"""Add voice_agent_presence and ai_call_transfer_sessions tables (additive only)

Revision ID: 20261009_call_transfer_presence
Revises: 20261005_demo_requests
Create Date: 2026-10-09 11:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '20261009_call_transfer_presence'
down_revision: Union[str, Sequence[str], None] = '20261005_demo_requests'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create voice_agent_presence table
    try:
        op.create_table(
            'voice_agent_presence',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('business_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('agent_id', sa.String(length=36), nullable=False, unique=True, index=True),
            sa.Column('status', sa.String(length=30), server_default='AVAILABLE', index=True),
            sa.Column('current_call_id', sa.String(length=64), nullable=True),
            sa.Column('locked_until', sa.DateTime(), nullable=True),
            sa.Column('last_status_change', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
    except Exception:
        pass

    # 2. Create ai_call_transfer_sessions table
    try:
        op.create_table(
            'ai_call_transfer_sessions',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('business_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('call_id', sa.String(length=64), nullable=False, index=True),
            sa.Column('campaign_id', sa.String(length=36), nullable=True, index=True),
            sa.Column('lead_id', sa.String(length=36), nullable=True, index=True),
            sa.Column('customer_name', sa.String(length=255), server_default='Customer'),
            sa.Column('customer_phone', sa.String(length=50), server_default=''),
            sa.Column('from_agent_name', sa.String(length=255), server_default='CallZenza AI Agent'),
            sa.Column('assigned_voice_agent_id', sa.String(length=36), nullable=True, index=True),
            sa.Column('assigned_voice_agent_name', sa.String(length=255), nullable=True),
            sa.Column('intent_detected_at', sa.DateTime(), nullable=True),
            sa.Column('ringing_at', sa.DateTime(), nullable=True),
            sa.Column('accepted_at', sa.DateTime(), nullable=True),
            sa.Column('ai_disconnected_at', sa.DateTime(), nullable=True),
            sa.Column('completed_at', sa.DateTime(), nullable=True),
            sa.Column('status', sa.String(length=50), server_default='REQUESTED', index=True),
            sa.Column('failure_reason', sa.String(length=255), nullable=True),
            sa.Column('transcript_snapshot', sa.Text(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_table('ai_call_transfer_sessions')
    except Exception:
        pass
    try:
        op.drop_table('voice_agent_presence')
    except Exception:
        pass
