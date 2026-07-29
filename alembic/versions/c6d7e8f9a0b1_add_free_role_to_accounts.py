"""Add 'free' role and make it the default for accounts

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-07-28 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c6d7e8f9a0b1'
down_revision: Union[str, None] = 'b5c6d7e8f9a0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Widen the constraint BEFORE writing any 'free' rows, or the backfill
    # below fails against its own CHECK.
    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member','free')",
    )

    op.alter_column('accounts', 'role', server_default='free')

    # Backfill. 'member' now means "paying subscriber", but every existing row
    # holds it only because it used to be the default — so demote anyone who
    # does not have an entitling subscription. Admins are staff and are left
    # alone. Mirrors role_for_subscription() in src/db/models.py.
    #
    # Idempotent: re-running changes nothing once roles agree with Stripe.
    op.execute(
        """
        UPDATE accounts
           SET role = 'free'
         WHERE role <> 'admin'
           AND (subscription_status IS NULL
                OR subscription_status NOT IN ('active', 'trialing'))
        """
    )
    op.execute(
        """
        UPDATE accounts
           SET role = 'member'
         WHERE role <> 'admin'
           AND subscription_status IN ('active', 'trialing')
        """
    )


def downgrade() -> None:
    # 'free' is not a legal value under the old constraint, so those rows have
    # to move back to 'member' before it is restored.
    op.execute("UPDATE accounts SET role = 'member' WHERE role = 'free'")
    op.alter_column('accounts', 'role', server_default='member')
    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.create_check_constraint(
        'ck_accounts_role',
        'accounts',
        "role IN ('admin','member')",
    )
