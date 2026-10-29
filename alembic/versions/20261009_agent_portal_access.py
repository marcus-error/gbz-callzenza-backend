"""Add portal_user_agent_access table (additive only)

Revision ID: 20261009_agent_portal_access
Revises: 20261009_call_transfer_presence
Create Date: 2026-10-09 13:30:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


revision: str = '20261009_agent_portal_access'
down_revision: Union[str, Sequence[str], None] = '20261009_call_transfer_presence'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    try:
        op.create_table(
            'portal_user_agent_access',
            sa.Column('id', sa.String(length=36), primary_key=True),
            sa.Column('user_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('agent_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('business_id', sa.String(length=36), nullable=False, index=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
        )
    except Exception:
        pass


def downgrade() -> None:
    try:
        op.drop_table('portal_user_agent_access')
    except Exception:
        pass
