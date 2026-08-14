"""add app_settings table

Revision ID: b8c9d0e1f2a3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-13

Per-account, per-org application preferences backing the App Settings page
(default agent for Agent Chat, ElevenLabs voice). One row per
(account_id, org_id); default_agent_id is SET NULL on agent deletion so the
rest of the row survives the agent going away.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('default_agent_id', sa.Integer(), sa.ForeignKey('agents.id', ondelete='SET NULL'), nullable=True),
        sa.Column('elevenlabs_voice_id', sa.String(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('account_id', 'org_id', name='uq_app_settings_account_org'),
    )
    op.create_index(op.f('ix_app_settings_id'), 'app_settings', ['id'], unique=True)
    op.create_index(op.f('ix_app_settings_account_id'), 'app_settings', ['account_id'])
    op.create_index(op.f('ix_app_settings_org_id'), 'app_settings', ['org_id'])
    op.create_index(op.f('ix_app_settings_default_agent_id'), 'app_settings', ['default_agent_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_app_settings_default_agent_id'), table_name='app_settings')
    op.drop_index(op.f('ix_app_settings_org_id'), table_name='app_settings')
    op.drop_index(op.f('ix_app_settings_account_id'), table_name='app_settings')
    op.drop_index(op.f('ix_app_settings_id'), table_name='app_settings')
    op.drop_table('app_settings')
