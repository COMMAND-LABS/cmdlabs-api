"""read-only grace after a lapse

Revision ID: b6c7d8e9f0a2
Revises: a5b6c7d8e9f1

ONE TIMESTAMP IN, ONE STORED STATE OUT.

`organizations.status` held 'active' | 'read_only' and was enforced on every
write by deps.require_module. Nothing ever wrote it. Every one of the 276 orgs
on the platform sat at 'active', including the one whose subscription had been
cancelled — so a protection that read like policy had never once fired.

Deleting it is not a loss of behaviour, because there was none. What replaces
it is a single nullable timestamp on the ACCOUNT — when its subscription
stopped being an entitling one — and three states derived from that instant
plus the clock:

    ACTIVE   Stripe says paid            premium modules, writes allowed
    GRACE    within GRACE_DAYS           premium modules, READS ONLY
    LAPSED   past GRACE_DAYS             free modules,    writes allowed

WHY DERIVED RATHER THAN STORED. A stored state needs something to move it, and
"something" is either a scheduled job or a webhook that must fire at exactly
the right moment. Both can be missed, and a missed transition leaves a customer
locked out of a workspace they have paid for — with no symptom until they
complain. A comparison against a timestamp cannot be missed. It is the same
argument that removed the stored ceiling in d2e3f4a5b6c8, applied to the other
column billing was supposed to own and never touched.

BACKFILL. Deliberately none. Setting subscription_lapsed_at = now() for every
already-cancelled account would start a fresh two-week read-only window for
people who lapsed months ago — punishing them, today, for a column that did not
exist when they left. NULL reads as LAPSED (see plans_registry.billing_state),
which is where they already are: on the free plan, able to use it. Grace is a
courtesy extended at the moment of lapse, and for these there is no moment on
record to extend it from.

Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'b6c7d8e9f0a2'
down_revision = 'a5b6c7d8e9f1'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'accounts',
        sa.Column('subscription_lapsed_at', sa.DateTime(timezone=True),
                  nullable=True),
    )

    # Order matters: the CHECK references the column.
    op.drop_constraint('ck_organizations_status', 'organizations',
                       type_='check')
    op.drop_column('organizations', 'status')


def downgrade():
    op.add_column(
        'organizations',
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active'),
    )
    op.create_check_constraint(
        'ck_organizations_status', 'organizations',
        "status IN ('active','read_only')")
    op.drop_column('accounts', 'subscription_lapsed_at')
