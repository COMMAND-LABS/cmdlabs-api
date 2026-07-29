"""Rename the 'member' account role to 'premium' and track pending downgrades

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-07-29 05:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, None] = 'c6d7e8f9a0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen the constraint to accept BOTH names first, so the UPDATE below has
    # somewhere legal to land, then narrow it once no 'member' rows remain.
    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member','premium','free')",
    )

    op.execute("UPDATE accounts SET role = 'premium' WHERE role = 'member'")

    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','premium','free')",
    )

    # NOTE: accounts.role only. access_group_members.role is a different
    # concept ('admin' | 'member' within one group) and is left alone.

    # Whether a downgrade is scheduled. Stripe keeps the subscription 'active'
    # until the paid-up period ends, so this is the only way to tell "premium"
    # from "premium, but leaving".
    op.add_column(
        'accounts',
        sa.Column('subscription_cancel_at_period_end', sa.Boolean(),
                  nullable=False, server_default='false'),
    )


def downgrade() -> None:
    op.drop_column('accounts', 'subscription_cancel_at_period_end')

    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member','premium','free')",
    )
    op.execute("UPDATE accounts SET role = 'member' WHERE role = 'premium'")
    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member','free')",
    )
