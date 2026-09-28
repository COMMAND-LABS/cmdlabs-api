"""add accounts.subscription_cancel_at_period_end and the org.owner_transfer audit event

Revision ID: b49167452008
Revises: f2a3b4c5d6e7
Create Date: 2026-09-27

Records that a cancellation is SCHEDULED: Stripe keeps the subscription active
until the period ends, then deletes it. Without this the membership page kept
saying "Renews on <date>" for a customer who had already cancelled in the
billing portal. Written only by the Stripe webhook, like the other
subscription columns.

Also widens ck_access_grant_event_type with 'org.owner_transfer', written when
an owner hands their organization to another member.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# ck_access_grant_event_type as of the previous head, plus the new verb.
PREVIOUS = (
    'create',
    'revoke',
    'role_change',
    'member.add',
    'member.remove',
    'member.tier_change',
    'member.role_change',
    'member.invite',
    'member.invite_revoke',
    'member.invite_decline',
    'member.invite_resend',
    'org.create',
    'org.suspend',
    'org.restore',
    'org.ceiling_change',
    'org.rename',
    'tier.modules_change',
    'catalog.publish',
    'catalog.unpublish',
    'catalog.grant',
    'catalog.revoke',
    'super_admin.join',
    'space.create',
    'space.archive',
    'space.member_add',
    'space.member_remove',
    'space.request',
    'space.request_approve',
    'space.request_deny',
    'space.resource_add',
    'space.resource_remove',
)
EVENT_TYPES = PREVIOUS + ('org.owner_transfer',)


def _check_sql(types) -> str:
    return 'event_type IN (' + ','.join(f"'{t}'" for t in types) + ')'


revision: str = 'b49167452008'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Production already has this column: d7e8f9a0b1c2 originally added it
    # (514a60f) and was applied, then the add was deleted from that file
    # (82a303f). Fresh databases don't have it, so add it only when missing —
    # the existing definition (Boolean, NOT NULL, default false) is identical.
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('accounts')}
    if 'subscription_cancel_at_period_end' not in existing:
        op.add_column(
            'accounts',
            sa.Column('subscription_cancel_at_period_end', sa.Boolean(),
                      server_default=sa.false(), nullable=False),
        )
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(EVENT_TYPES))


def downgrade() -> None:
    # Relabel rather than delete: an audit row is evidence.
    op.execute("UPDATE access_grant_events SET event_type = 'org.ceiling_change' "
               "WHERE event_type = 'org.owner_transfer'")
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(PREVIOUS))
    op.drop_column('accounts', 'subscription_cancel_at_period_end')
