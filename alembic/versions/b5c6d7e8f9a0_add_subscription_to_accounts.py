"""Add Stripe subscription fields to accounts table

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-07-28 11:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # All nullable: an account with no subscription row in Stripe simply has
    # NULLs here, which reads as "not a paying member". Existing accounts are
    # therefore unsubscribed until a webhook says otherwise.
    op.add_column('accounts', sa.Column('stripe_subscription_id', sa.String(), nullable=True))
    op.add_column('accounts', sa.Column('subscription_status', sa.String(length=30), nullable=True))
    op.add_column(
        'accounts',
        sa.Column('subscription_current_period_end', sa.DateTime(timezone=True), nullable=True),
    )
    # Indexed because the webhook looks accounts up by subscription id, and
    # billing reports filter by status.
    op.create_index('ix_accounts_stripe_subscription_id', 'accounts', ['stripe_subscription_id'])
    op.create_index('ix_accounts_subscription_status', 'accounts', ['subscription_status'])
    # No CHECK constraint on subscription_status: Stripe owns that vocabulary.


def downgrade() -> None:
    op.drop_index('ix_accounts_subscription_status', table_name='accounts')
    op.drop_index('ix_accounts_stripe_subscription_id', table_name='accounts')
    op.drop_column('accounts', 'subscription_current_period_end')
    op.drop_column('accounts', 'subscription_status')
    op.drop_column('accounts', 'stripe_subscription_id')
