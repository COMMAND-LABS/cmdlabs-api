"""Add role to accounts table

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-28 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Every existing account becomes a plain 'member'; promoting the first
    # 'admin' is a deliberate, separate action.
    op.add_column(
        'accounts',
        sa.Column('role', sa.String(length=20), nullable=False, server_default='member'),
    )
    op.create_index('ix_accounts_role', 'accounts', ['role'])
    # CHECK rather than a PG enum so the allowed set can change without an
    # enum migration. Mirrors ACCOUNT_ROLES in src/db/models.py.
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member')",
    )


def downgrade() -> None:
    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.drop_index('ix_accounts_role', table_name='accounts')
    op.drop_column('accounts', 'role')
