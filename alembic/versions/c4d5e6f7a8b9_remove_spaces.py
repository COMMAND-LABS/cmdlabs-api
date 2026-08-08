"""remove spaces: back to one container

WHY
---
Spaces were the platform's SECOND container: an org held private data keyed by
`org_id`, a space held SHARED content whose members came from many orgs. The
idea was sound and the schema said so plainly. It is being removed to make the
platform small enough to reason about in one sitting, and it is expected back.

WHAT GOES WITH IT — read this before running it
-----------------------------------------------
This is not only five tables. Three capabilities disappear:

  1. CROSS-ORG SHARING. `space_resources` was the ONLY way an agent or a
     knowledge base reached somebody outside the owning org — arm 3 of
     org_scope.visible_resource_predicate, mirrored in both services. After
     this, reaching another org's resource means JOINING that org. Anyone
     depending on a share loses access the moment this runs.
  2. COURSES IN A SPACE. `courses` was dual-homed. Rows living in a space have
     no org to fall back to and are DELETED below — they cannot be expressed
     once the column is gone, and inventing an org for them would hand somebody
     content the space owner never gave that org.
  3. PAID SPACE MEMBERSHIP. `space_tiers.stripe_price_id` was a per-space
     paywall. Nothing here touches Stripe: a subscription created against one
     of those prices keeps billing after this migration drops the row that
     explains it. CHECK FOR LIVE SUBSCRIPTIONS BEFORE RUNNING THIS IN PROD.

`e3f4a5b6c7d9` recorded two live space_resources rows reaching four accounts in
a client org. If that is still true where this runs, those four people lose an
agent and its knowledge base. There is no in-place substitute — the nearest
equivalent is an AccessGrant each, which requires them to be members of the
owning org.

WHAT IS DELIBERATELY LEFT ALONE
-------------------------------
`ck_access_grant_event_type` still admits the nine `space.*` event types, and
the rows carrying them stay exactly where they are. Narrowing the constraint
would fail against existing rows, and passing that by deleting them first would
make the audit log assert those events never happened. An audit log that
rewrites its own history when a feature is removed is not an audit log.

DOWNGRADE RESTORES THE SCHEMA, NOT THE DATA. It recreates all five tables empty
and re-adds courses.space_id. Every space, membership, join request, tier and
share is gone for good. Take a dump first if that matters.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- courses go back to ONE home -------------------------------------
    # Deleted rather than rehomed. A space course belongs to the space's
    # audience, and the only org available to move it into is the owner's —
    # which would publish it to a tenant whose members were never given it.
    # Losing the row is the honest outcome; the same call c1d2e3f4a5b7's own
    # downgrade made.
    op.execute("DELETE FROM courses WHERE space_id IS NOT NULL")

    op.drop_constraint('uq_course_space_key', 'courses', type_='unique')
    op.drop_constraint('ck_courses_one_home', 'courses', type_='check')
    op.drop_index('ix_courses_space_id', table_name='courses')
    op.drop_constraint('fk_courses_space_id', 'courses', type_='foreignkey')
    op.drop_column('courses', 'space_id')
    # The dual home is what made this nullable. With one container left, NOT
    # NULL is the constraint — and it is a stronger statement than the CHECK it
    # replaces, because it cannot be satisfied by a row with no home at all.
    op.alter_column('courses', 'org_id', existing_type=sa.Integer(),
                    nullable=False)

    # --- the container itself --------------------------------------------
    # Child-first. Every one of these FKs is ON DELETE CASCADE, so the order is
    # belt and braces rather than strictly required — but a drop that depends on
    # cascade semantics to succeed is one that fails confusingly if a future
    # column is added without them.
    op.drop_table('space_resources')
    op.drop_table('space_join_requests')
    op.drop_table('space_members')
    op.drop_table('space_tiers')
    op.drop_table('spaces')


def downgrade() -> None:
    """Recreate the schema. The rows are NOT coming back — see the docstring."""
    op.create_table(
        'spaces',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        # SET NULL rather than CASCADE: a space outliving the account that
        # created it needs a new owner, not members who silently lose
        # everything they joined for.
        sa.Column('owner_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True),
        # ATTRIBUTION, NEVER TENANCY. Who is accountable and billed for this
        # space. Reading it to authorize access to a space's content is the one
        # mistake that turns this design into a data leak.
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
        sa.CheckConstraint("join_policy IN ('invite','request','open')",
                           name='ck_spaces_join_policy'),
        sa.CheckConstraint("status IN ('active','archived')",
                           name='ck_spaces_status'),
    )
    # No slug: c1d2e3f4a5b7 dropped it and this restores the shape as it was
    # when spaces were removed, not as they were first created.
    op.create_index('ix_spaces_owner_account_id', 'spaces', ['owner_account_id'])
    op.create_index('ix_spaces_owner_org_id', 'spaces', ['owner_org_id'])
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
        sa.UniqueConstraint('space_id', 'account_id',
                            name='uq_space_join_request'),
        sa.CheckConstraint("status IN ('pending','approved','denied')",
                           name='ck_space_join_request_status'),
    )
    op.create_index('ix_space_join_requests_space_id', 'space_join_requests',
                    ['space_id'])
    op.create_index('ix_space_join_requests_account_id', 'space_join_requests',
                    ['account_id'])

    op.create_table(
        'space_resources',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('space_id', sa.Integer(),
                  sa.ForeignKey('spaces.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('resource_type', sa.String(20), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('added_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('space_id', 'resource_type', 'resource_id',
                            name='uq_space_resource'),
        # Agents and knowledge bases only. A CREDENTIAL is an API key with a
        # bill attached and may never reach a cross-org audience.
        sa.CheckConstraint("resource_type IN ('agent','vector_store')",
                           name='ck_space_resource_type'),
    )
    op.create_index('ix_space_resources_space_id', 'space_resources',
                    ['space_id'])

    # --- courses become dual-homed again ---------------------------------
    op.alter_column('courses', 'org_id', existing_type=sa.Integer(),
                    nullable=True)
    op.add_column('courses', sa.Column('space_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_courses_space_id', 'courses', 'spaces',
                          ['space_id'], ['id'], ondelete='CASCADE')
    op.create_index('ix_courses_space_id', 'courses', ['space_id'])
    op.create_check_constraint(
        'ck_courses_one_home', 'courses',
        '(org_id IS NULL) <> (space_id IS NULL)',
    )
    # Postgres treats NULLs as distinct, so this constrains only the rows that
    # actually live in a space, exactly as uq_course_org_key does for orgs.
    op.create_unique_constraint('uq_course_space_key', 'courses',
                                ['space_id', 'course_key'])
