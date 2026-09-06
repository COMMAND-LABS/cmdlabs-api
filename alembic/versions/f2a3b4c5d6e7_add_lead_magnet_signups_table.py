"""add lead_magnet_signups table

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-06

Public email capture for the /resources lead magnet pages. Account-less rows
like `feedback`, and append-only: no unique constraint on (slug, email), so a
repeat request simply re-sends the email and stats count distinct emails. The
slug is a key into the code registry (src/config/lead_magnets_registry.py),
not a foreign key, so a retired magnet keeps its history.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'lead_magnet_signups',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('attribution', postgresql.JSONB(), nullable=True),
        sa.Column('user_agent', sa.String(512), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f('ix_lead_magnet_signups_id'), 'lead_magnet_signups', ['id'], unique=True)
    op.create_index(op.f('ix_lead_magnet_signups_slug'), 'lead_magnet_signups', ['slug'])
    op.create_index(op.f('ix_lead_magnet_signups_email'), 'lead_magnet_signups', ['email'])
    op.create_index('ix_lead_magnet_signups_slug_created_at', 'lead_magnet_signups', ['slug', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_lead_magnet_signups_slug_created_at', table_name='lead_magnet_signups')
    op.drop_index(op.f('ix_lead_magnet_signups_email'), table_name='lead_magnet_signups')
    op.drop_index(op.f('ix_lead_magnet_signups_slug'), table_name='lead_magnet_signups')
    op.drop_index(op.f('ix_lead_magnet_signups_id'), table_name='lead_magnet_signups')
    op.drop_table('lead_magnet_signups')
