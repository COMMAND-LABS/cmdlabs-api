"""add_name_to_accounts

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-16

Optional display name on accounts. Accounts are created from an email alone
(verify-code signup), so the column is nullable and NULL means "never
provided" — no backfill.
"""
from typing import Sequence, Union
import sqlalchemy as sa
from alembic import op

revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd0e1f2a3b4c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('name', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('accounts', 'name')
