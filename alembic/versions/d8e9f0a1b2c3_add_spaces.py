"""add spaces: the second container

An org is a tenant and holds private data. A SPACE holds shared content and its
members come from many orgs. Every row in the platform lives in exactly one of
the two — see src/db/space_models.py for the argument.

Nothing is dual-homed yet. This migration adds the container, its membership,
its tiers and its join requests; moving courses and knowledge bases to
`CHECK ((org_id IS NULL) <> (space_id IS NULL))` is a separate change, so that
the boundary can be reviewed on its own before any content moves through it.

WHAT IS DELIBERATELY ABSENT: any foreign key or column that would let a space's
content be reached through its owner's org. `spaces.owner_org_id` exists for
billing and accountability and is never read to authorize a read.

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd8e9f0a1b2c3'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in step with services/audit.py. Copied rather than imported: a migration
# is a point-in-time snapshot and must not follow the app as it changes.
EVENT_TYPES = [
    'create', 'revoke', 'role_change',
    'member.add', 'member.remove', 'member.tier_change',
    'org.create', 'org.suspend', 'org.restore', 'org.ceiling_change',
    'org.rename',
    'tier.modules_change',
    'catalog.publish', 'catalog.unpublish', 'catalog.grant', 'catalog.revoke',
    'staff.join',
    'space.create', 'space.archive',
    'space.member_add', 'space.member_remove',
    'space.request', 'space.request_approve', 'space.request_deny',
]

PREVIOUS = [v for v in EVENT_TYPES if not v.startswith('space.')]


def _check_sql(values) -> str:
    return "event_type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        'spaces',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        # SET NULL rather than CASCADE: a space outliving the account that
        # created it is a space that needs a new owner, not one whose members
        # silently lose everything they joined for.
        sa.Column('owner_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('owner_org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('discoverable', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('join_policy', sa.String(20), nullable=False,
                  server_default='invite'),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('slug', name='uq_spaces_slug'),
        sa.CheckConstraint("join_policy IN ('invite','request','open')",
                           name='ck_spaces_join_policy'),
        sa.CheckConstraint("status IN ('active','archived')",
                           name='ck_spaces_status'),
    )
    op.create_index('ix_spaces_owner_account_id', 'spaces', ['owner_account_id'])
    op.create_index('ix_spaces_owner_org_id', 'spaces', ['owner_org_id'])
    # The browse page's only query: discoverable, active, newest first.
    op.create_index('ix_spaces_discoverable', 'spaces',
                    ['discoverable', 'status'])

    op.create_table(
        'space_tiers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('space_id', sa.Integer(),
                  sa.ForeignKey('spaces.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('tier_key', sa.String(64), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('stripe_price_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('space_id', 'tier_key', name='uq_space_tier_key'),
    )
    op.create_index('ix_space_tiers_space_id', 'space_tiers', ['space_id'])

    op.create_table(
        'space_members',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('space_id', sa.Integer(),
                  sa.ForeignKey('spaces.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('tier_key', sa.String(64), nullable=False,
                  server_default='member'),
        sa.Column('is_owner', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('granted_by', sa.String(20), nullable=False,
                  server_default='grant'),
        sa.Column('invited_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('space_id', 'account_id', name='uq_space_member'),
        sa.CheckConstraint("granted_by IN ('grant','subscription','request')",
                           name='ck_space_member_granted_by'),
    )
    op.create_index('ix_space_members_space_id', 'space_members', ['space_id'])
    op.create_index('ix_space_members_account_id', 'space_members',
                    ['account_id'])

    op.create_table(
        'space_join_requests',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('space_id', sa.Integer(),
                  sa.ForeignKey('spaces.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='pending'),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('decided_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        # One row per (space, account), reused when somebody asks again after
        # being turned down — so "have they been refused before?" stays a
        # single lookup and a denied applicant cannot flood the owner's queue.
        sa.UniqueConstraint('space_id', 'account_id',
                            name='uq_space_join_request'),
        sa.CheckConstraint("status IN ('pending','approved','denied')",
                           name='ck_space_join_request_status'),
    )
    op.create_index('ix_space_join_requests_space_id', 'space_join_requests',
                    ['space_id'])
    op.create_index('ix_space_join_requests_account_id', 'space_join_requests',
                    ['account_id'])

    # The audit log's vocabulary is a CHECK constraint, so a new event type has
    # to be admitted before it can be written. Widened, never replaced: every
    # value already in use survives.
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(EVENT_TYPES))


def downgrade() -> None:
    # Collapse rather than delete, the same way a5b6c7d8e9f0 does: an audit row
    # is evidence, and a downgrade that erases history is a downgrade somebody
    # runs to erase history. Space events relabel to the nearest surviving
    # membership verb before the tables holding their subjects go.
    op.execute(
        "UPDATE access_grant_events SET event_type = 'member.add' "
        "WHERE event_type IN ('space.create','space.member_add',"
        "'space.request','space.request_approve')"
    )
    op.execute(
        "UPDATE access_grant_events SET event_type = 'member.remove' "
        "WHERE event_type IN ('space.archive','space.member_remove',"
        "'space.request_deny')"
    )
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(PREVIOUS))

    op.drop_table('space_join_requests')
    op.drop_table('space_members')
    op.drop_table('space_tiers')
    op.drop_table('spaces')
