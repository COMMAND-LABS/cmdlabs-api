"""tiers become roles

WHAT CHANGES
------------
organization_members.tier_key named a row in organization_tiers: a per-org,
owner-editable set of module keys. It becomes `role`, one of two platform-wide
constants defined in src/config/roles_registry.py:

    manager           the core team — everything the org's plan allows
    community_member  people the org SERVES — a fixed, small allowlist

organization_tiers is dropped entirely.

WHY
---
Three things get better and one gets worse, and the trade was made deliberately.

Better: "what can this person do?" now means the same in every org, so it can
be answered without reading that org's config. A role cannot be edited, so the
self-upgrade hole plans_registry documents — a free user rewriting their own
tier, held shut only by clamp_to_ceiling — is closed by construction. And there
is nothing per-org left to seed, so no org can start life with a broken matrix.

Worse: an org owner can no longer define or sell their own bundles.
organization_tiers.stripe_price_id existed for that and was never used.

EVERY NON-OWNER DROPS TO community_member
-----------------------------------------
This is the instruction, and it is deliberately NOT a preserving migration.
Least privilege wins over continuity here: nobody silently keeps CRM access
because a tier they were on happened to include it.

    IT WILL TAKE THE CRM AWAY FROM REAL COLLABORATORS.

An owner re-promotes them in Settings -> Members, or via
PUT /api/organizations/members/{account_id} with {"role": "manager"}. Before
running this on live data, it is worth listing who is about to change:

    SELECT o.name, a.email, m.tier_key
    FROM organization_members m
    JOIN organizations o ON o.id = m.org_id
    JOIN accounts a ON a.id = m.account_id
    WHERE m.account_id IS DISTINCT FROM o.owner_account_id;

OWNERS ARE INCLUDED, AND IT DOES NOT MATTER TODAY
-------------------------------------------------
An owner's role is INERT — services/modules.effective_modules returns the whole
ceiling for them before it ever looks at the role. So an owner sitting on
community_member reaches everything regardless.

It WILL matter the day ownership transfer ships: transferring away from an
account would leave them on community_member, and transferring TO an account
whose role was never raised leaves the outgoing owner with nothing. Whatever
implements transfer must set both parties' roles explicitly rather than assume
this migration left something sensible behind.

THE AUDIT LOG IS NOT REWRITTEN. Rows carrying 'member.tier_change' and
'tier.modules_change' stay exactly as they are, and the CHECK constraint still
admits them — the same choice made when spaces were removed. New role changes
write 'member.role_change'.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

ROLE_MANAGER = 'manager'
ROLE_COMMUNITY = 'community_member'

# Copied, not imported. A migration is a point-in-time snapshot: importing the
# live registry would make this file's behaviour change as the app changes,
# which is the one thing a migration may not do.
EVENT_TYPES_ADDED = ('member.role_change',)


def upgrade() -> None:
    # --- the column -------------------------------------------------------
    # Added nullable so the backfill can run, then tightened. A server_default
    # is set as well: it is what protects a row written by an older process
    # still in flight during the deploy, which would otherwise fail the NOT
    # NULL and take an invite down with it.
    op.add_column(
        'organization_members',
        sa.Column('role', sa.String(32), nullable=True,
                  server_default=ROLE_COMMUNITY),
    )

    # EVERY row, owners included. See the docstring: an owner's role is inert
    # today, and pretending otherwise here would put a value in the column that
    # implies it grants something.
    op.execute(
        f"UPDATE organization_members SET role = '{ROLE_COMMUNITY}' "
        "WHERE role IS NULL"
    )

    op.alter_column('organization_members', 'role',
                    existing_type=sa.String(32), nullable=False)
    op.create_check_constraint(
        'ck_org_member_role',
        'organization_members',
        f"role IN ('{ROLE_MANAGER}', '{ROLE_COMMUNITY}')",
    )

    # --- what it replaces -------------------------------------------------
    op.drop_column('organization_members', 'tier_key')
    op.drop_table('organization_tiers')

    # --- the audit vocabulary --------------------------------------------
    # WIDENED, never narrowed. 'member.tier_change' and 'tier.modules_change'
    # stay admitted because rows already carry them, and a log that drops
    # values when a feature is removed is asserting those events never
    # happened.
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_event_type', 'access_grant_events',
        _event_check(_existing_event_types() + list(EVENT_TYPES_ADDED)),
    )


def downgrade() -> None:
    """Recreate organization_tiers and tier_key. Seeds a usable matrix.

    NOT a restoration of what was there. The original per-org tier definitions
    are gone — this recreates the two tiers a fresh org used to be seeded with
    and puts everybody on 'member', which is the state a newly created org had.
    An org that had hand-configured tiers does not get them back.
    """
    op.create_table(
        'organization_tiers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('tier_key', sa.String(64), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('modules', sa.dialects.postgresql.JSONB(), nullable=False,
                  server_default='[]'),
        sa.Column('stripe_price_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.UniqueConstraint('org_id', 'tier_key', name='uq_org_tier_key'),
    )
    op.create_index('ix_organization_tiers_org_id', 'organization_tiers',
                    ['org_id'])

    # One 'owner' and one 'member' tier per org, matching what the old signup
    # seeded. 'member' starts empty, as it did: an invited person got what the
    # owner deliberately checked, never a default.
    op.execute("""
        INSERT INTO organization_tiers (org_id, tier_key, label, modules)
        SELECT id, 'owner', 'Owner', '[]'::jsonb FROM organizations
        UNION ALL
        SELECT id, 'member', 'Member', '[]'::jsonb FROM organizations
    """)

    op.add_column(
        'organization_members',
        sa.Column('tier_key', sa.String(64), nullable=True,
                  server_default='member'),
    )
    op.execute("UPDATE organization_members SET tier_key = 'member'")
    op.alter_column('organization_members', 'tier_key',
                    existing_type=sa.String(64), nullable=False)

    op.drop_constraint('ck_org_member_role', 'organization_members',
                       type_='check')
    op.drop_column('organization_members', 'role')

    # The event-type CHECK is left WIDE. Narrowing it would fail against any
    # member.role_change row written while this migration was applied, and
    # deleting those rows to make it pass is exactly what an audit log is for
    # preventing.


# ── helpers ──────────────────────────────────────────────────────────────────
# The event vocabulary is spelled out rather than read from the app, for the
# snapshot reason above. Kept in one place so the two constraint rewrites above
# cannot disagree.

def _existing_event_types() -> list:
    return [
        'create', 'revoke', 'role_change',
        'member.add', 'member.remove', 'member.tier_change',
        'org.create', 'org.suspend', 'org.restore', 'org.ceiling_change',
        'org.rename',
        'tier.modules_change',
        'catalog.publish', 'catalog.unpublish', 'catalog.grant',
        'catalog.revoke',
        'super_admin.join',
        'space.create', 'space.archive',
        'space.member_add', 'space.member_remove',
        'space.request', 'space.request_approve', 'space.request_deny',
        'space.resource_add', 'space.resource_remove',
    ]


def _event_check(values) -> str:
    joined = ", ".join(f"'{v}'" for v in values)
    return f"event_type IN ({joined})"
