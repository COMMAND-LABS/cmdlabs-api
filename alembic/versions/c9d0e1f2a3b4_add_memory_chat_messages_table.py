"""add memory_chat_messages table

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-08-14

Backing store for the Memory Chat demo: one rolling conversation per
(account, org). Rows are only ever appended or explicitly cleared — the
context-window "drop" is a read-time computation, never a delete.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'memory_chat_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('account_id', sa.Integer(), sa.ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(length=10), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(op.f('ix_memory_chat_messages_id'), 'memory_chat_messages', ['id'], unique=True)
    op.create_index(op.f('ix_memory_chat_messages_account_id'), 'memory_chat_messages', ['account_id'])
    op.create_index(op.f('ix_memory_chat_messages_org_id'), 'memory_chat_messages', ['org_id'])


def downgrade() -> None:
    op.drop_index(op.f('ix_memory_chat_messages_org_id'), table_name='memory_chat_messages')
    op.drop_index(op.f('ix_memory_chat_messages_account_id'), table_name='memory_chat_messages')
    op.drop_index(op.f('ix_memory_chat_messages_id'), table_name='memory_chat_messages')
    op.drop_table('memory_chat_messages')
