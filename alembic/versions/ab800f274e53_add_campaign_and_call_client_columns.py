"""add_campaign_and_call_client_columns

Revision ID: ab800f274e53
Revises: 
Create Date: 2026-08-28 11:19:53.756735

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ab800f274e53'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.add_column(sa.Column("agent_type", sa.String(length=50), server_default="AI_VOICE", nullable=False))
        batch_op.add_column(sa.Column("client_name", sa.String(length=255), server_default="", nullable=False))
        batch_op.add_column(sa.Column("client_phone", sa.String(length=50), server_default="", nullable=False))
        batch_op.alter_column("agent_id", existing_type=sa.String(length=36), nullable=True)

    with op.batch_alter_table("calls") as batch_op:
        batch_op.add_column(sa.Column("agent_type", sa.String(length=50), server_default="AI_VOICE", nullable=False))
        batch_op.add_column(sa.Column("client_name", sa.String(length=255), server_default="", nullable=False))
        batch_op.add_column(sa.Column("client_phone", sa.String(length=50), server_default="", nullable=False))
        batch_op.add_column(sa.Column("conference_sid", sa.String(length=64), nullable=True))

    with op.batch_alter_table("campaign_leads") as batch_op:
        batch_op.add_column(sa.Column("assigned_expert_id", sa.String(length=36), nullable=True))

    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.add_column(sa.Column("details", sa.JSON(), server_default="{}", nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("audit_logs") as batch_op:
        batch_op.drop_column("details")

    with op.batch_alter_table("campaign_leads") as batch_op:
        batch_op.drop_column("assigned_expert_id")

    with op.batch_alter_table("calls") as batch_op:
        batch_op.drop_column("conference_sid")
        batch_op.drop_column("client_phone")
        batch_op.drop_column("client_name")
        batch_op.drop_column("agent_type")

    with op.batch_alter_table("campaigns") as batch_op:
        batch_op.drop_column("client_phone")
        batch_op.drop_column("client_name")
        batch_op.drop_column("agent_type")

