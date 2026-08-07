"""split staff out of accounts.role, and drop the role

`role` was doing two unrelated jobs in one column:

    'admin'            staff — granted out of band, read twice, for one thing
    'premium'|'free'   a CACHE of what Stripe says

The second job was the problem. Entitlement is read from `subscription_status`
everywhere it matters, so the cached value was a duplicate that could disagree
with the fact — which is why role_for_subscription() and a whole
sync_account_roles script existed to keep dragging it back into line. A column
that needs a reconciliation script is a column that is derived.

After this:

    is_staff = True/False           stored, granted out of band, one job
    plan     = free | premium       DERIVED per request from subscription_status
                                    (config/plans_registry.plan_for_account)

Nothing is lost. Anything that asked "is this person paying?" already had a
better answer available, and now has only that one.

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f0a1b2c3d4e5'
down_revision: Union[str, None] = 'e9f0a1b2c3d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('accounts', sa.Column('is_staff', sa.Boolean(),
                                        nullable=False,
                                        server_default=sa.text('false')))
    op.create_index('ix_accounts_is_staff', 'accounts', ['is_staff'])

    # The only value of `role` anybody read.
    op.execute("UPDATE accounts SET is_staff = true WHERE role = 'admin'")

    op.drop_constraint('ck_accounts_role', 'accounts', type_='check')
    op.drop_index('ix_accounts_role', table_name='accounts')
    op.drop_column('accounts', 'role')


def downgrade() -> None:
    op.add_column('accounts', sa.Column('role', sa.String(20), nullable=False,
                                        server_default='free'))
    op.create_index('ix_accounts_role', 'accounts', ['role'])
    op.create_check_constraint(
        'ck_accounts_role', 'accounts', "role IN ('admin','premium','free')")

    # Rebuilt from the two facts it used to conflate. Staff first, so an
    # account that is both staff and subscribed comes back as staff — which is
    # what role_for_subscription() did.
    op.execute("""
        UPDATE accounts SET role = 'premium'
         WHERE subscription_status IN ('active','trialing')
    """)
    op.execute("UPDATE accounts SET role = 'admin' WHERE is_staff = true")

    op.drop_index('ix_accounts_is_staff', table_name='accounts')
    op.drop_column('accounts', 'is_staff')
