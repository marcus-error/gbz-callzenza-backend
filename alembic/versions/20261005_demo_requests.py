"""Add demo_requests table and notifications reference_id

Revision ID: 20261005_demo_requests
Revises: 20260913_multitenant
Create Date: 2026-10-05 14:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '20261005_demo_requests'
down_revision: Union[str, Sequence[str], None] = '20260913_multitenant'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create demo_requests table
    try:
        op.create_table(
            'demo_requests',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('organization_name', sa.String(length=255), nullable=False),
            sa.Column('email', sa.String(length=255), nullable=False, index=True),
            sa.Column('status', sa.String(length=50), server_default='NEW', index=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=True),
        )
    except Exception:
        pass

    # 2. Add reference_id to notifications
    try:
        op.add_column('notifications', sa.Column('reference_id', sa.String(length=255), nullable=True))
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_column('notifications', 'reference_id')
    except Exception:
        pass
    try:
        op.drop_table('demo_requests')
    except Exception:
        pass
