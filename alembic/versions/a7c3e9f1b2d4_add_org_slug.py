"""add an optional organization slug and the org.slug_change audit event

Revision ID: a7c3e9f1b2d4
Revises: e5b7c9d1a2f3
Create Date: 2026-10-07

Organizations get an OPTIONAL public address: /org/<slug>/login and
/org/<slug>/signup show the org's name and land a MEMBER in it after sign-in.

NOT THE SLUG f4a5b6c7d8f0 REMOVED. That one was immutable, assigned at the
moment nobody had a reason to choose a name, doubled as `is_personal`, and
identified the platform's own org by value. This one is:

  optional   NULL for every org until its owner picks one. Personal
             workspaces are never assigned one; nothing is backfilled.
  mutable    the owner can change or clear it. Links already shared break,
             and the settings screen says so.
  not an id  every route still keys on organizations.id. The slug is only
             resolved to an id by the public lookup and by /verify-code.

Also widens ck_access_grant_event_type with 'org.slug_change', written when
the address is set, changed or cleared.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# ck_access_grant_event_type as of the previous head (b49167452008), plus the
# new verb.
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
    'org.owner_transfer',
)
EVENT_TYPES = PREVIOUS + ('org.slug_change',)


def _check_sql(types) -> str:
    return 'event_type IN (' + ','.join(f"'{t}'" for t in types) + ')'


revision: str = 'a7c3e9f1b2d4'
down_revision: Union[str, None] = 'e5b7c9d1a2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('organizations',
                  sa.Column('slug', sa.String(63), nullable=True))
    # Same constraint name the old column carried. Many NULLs are fine under a
    # Postgres unique constraint, which is what "optional" relies on.
    op.create_unique_constraint('uq_organizations_slug', 'organizations',
                                ['slug'])

    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(EVENT_TYPES))


def downgrade() -> None:
    # Relabel rather than delete: an audit row is evidence.
    op.execute("UPDATE access_grant_events SET event_type = 'org.rename' "
               "WHERE event_type = 'org.slug_change'")
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(PREVIOUS))

    op.drop_constraint('uq_organizations_slug', 'organizations',
                       type_='unique')
    op.drop_column('organizations', 'slug')
