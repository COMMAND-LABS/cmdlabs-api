"""Add organizations, memberships, and tiers; move every account into the root org

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-08-04 10:00:00.000000

Introduces the tenant. Nothing reads these tables yet — this migration is
deliberately invisible at runtime.

The key property: the root org is created with data_scope='personal', so every
existing account keeps seeing exactly its own rows once the CRM tables are
flipped to org scoping in a later phase. The scoping clause becomes

    org_id == <root> AND (data_scope == 'shared' OR created_by == me)

and for root that reduces to `created_by == me`, which is today's behavior.
That is what makes the later read-flip a provable no-op.

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'e8f9a0b1c2d3'
down_revision: Union[str, None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Canonical module keys. Stable identifiers, deliberately NOT the display names
# in cmdlabs-ui/src/config/navigation.tsx — a module is granted by key, so
# renaming "Deals" to "Pipeline" in the UI must not revoke anyone's access.
#
# Inlined rather than imported: a migration is a snapshot of the schema at a
# point in time and must keep working when the app's config later changes.
ALL_MODULES = [
    "home", "agents", "contacts", "contact_lists", "companies", "deals",
    "prompts", "agent_chat", "knowledge_bases", "access", "credentials",
    "email_templates", "email_campaigns", "analytics", "membership",
    "settings", "organization",
]

# Seeded to match today's hardcoded lists in cmdlabs-ui/src/config/roles.ts, so
# nothing visibly changes for existing users on the day this ships.
FREE_MODULES = ["home", "membership", "settings"]
PREMIUM_MODULES = ["agents", "agent_chat", "credentials", "membership", "settings"]
# org_owner additionally reaches the org admin surface (tiers x modules matrix).
ORG_OWNER_MODULES = PREMIUM_MODULES + ["organization"]


def upgrade() -> None:
    # ---------------------------------------------------------------- tables
    op.create_table(
        'organizations',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('slug', sa.String(64), nullable=False),
        sa.Column('name', sa.String(255), nullable=False),
        # Nullable so this migration works against an empty database (a fresh
        # CI run has no accounts to own the root org). Set by the backfill
        # below whenever an account exists.
        sa.Column('owner_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        # IMMUTABLE after creation. Flipping root from 'personal' to 'shared'
        # would expose every user's private contacts to every other user in a
        # single UPDATE. There is no API path that writes this column.
        sa.Column('data_scope', sa.String(20), nullable=False,
                  server_default='personal'),
        # The ceiling: which modules this org may use at all. Bespoke per org —
        # there is no plan table. An org owner distributes a subset of this to
        # their own tiers and can never exceed it.
        sa.Column('granted_modules', postgresql.JSONB(), nullable=False,
                  server_default='[]'),
        sa.Column('status', sa.String(20), nullable=False,
                  server_default='active'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('slug', name='uq_organizations_slug'),
        sa.CheckConstraint("data_scope IN ('personal','shared')",
                           name='ck_organizations_data_scope'),
        sa.CheckConstraint("status IN ('active','read_only')",
                           name='ck_organizations_status'),
    )

    op.create_table(
        'organization_tiers',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('tier_key', sa.String(64), nullable=False),
        sa.Column('label', sa.String(255), nullable=False),
        sa.Column('modules', postgresql.JSONB(), nullable=False,
                  server_default='[]'),
        # Populated only when an org owner sells this tier through their own
        # connected Stripe account. Unused until that ships; present now so it
        # needs no schema change then.
        sa.Column('stripe_price_id', sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('org_id', 'tier_key', name='uq_org_tier_key'),
    )

    op.create_table(
        'organization_members',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('tier_key', sa.String(64), nullable=False),
        # How this member got their tier. 'subscription' is owned by the Stripe
        # webhook and lapses; 'grant' is set by an owner and is NEVER touched by
        # any webhook. That distinction is the whole "comp a client" feature.
        sa.Column('granted_by', sa.String(20), nullable=False,
                  server_default='grant'),
        # A bypass, not a stored set of grants: an owner always reaches every
        # module their org's ceiling allows. Storing the owner's modules would
        # let a bad save lock them out of the page that fixes it.
        sa.Column('is_owner', sa.Boolean(), nullable=False,
                  server_default=sa.text('false')),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('org_id', 'account_id', name='uq_org_member'),
        sa.CheckConstraint("granted_by IN ('subscription','grant')",
                           name='ck_org_member_granted_by'),
    )

    # --------------------------------------------------------------- columns
    op.add_column('accounts', sa.Column(
        'default_org_id', sa.Integer(),
        sa.ForeignKey('organizations.id', ondelete='SET NULL'), nullable=True))
    op.create_index('ix_accounts_default_org_id', 'accounts', ['default_org_id'])

    # The grant machinery becomes intra-org. Without org_id here, a grant
    # between two accounts that later land in different orgs is a live
    # cross-org read path — the one thing the tenancy boundary must prevent.
    for table in ('access_groups', 'access_grants', 'access_grant_events'):
        op.add_column(table, sa.Column(
            'org_id', sa.Integer(),
            sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            nullable=True))
        op.create_index(f'ix_{table}_org_id', table, ['org_id'])

    # -------------------------------------------------------------- backfill
    # Idempotent throughout: re-running changes nothing.

    # Root org. Owner is the lowest-id staff account, else the lowest-id account
    # of any kind, else NULL on an empty database.
    op.execute(
        """
        INSERT INTO organizations
            (slug, name, owner_account_id, data_scope, granted_modules, status)
        SELECT
            'root',
            'CMD LABS',
            (SELECT id FROM accounts
              ORDER BY (role = 'admin') DESC, id ASC
              LIMIT 1),
            'personal',
            '%s'::jsonb,
            'active'
        WHERE NOT EXISTS (SELECT 1 FROM organizations WHERE slug = 'root')
        """
        % _json_array(ALL_MODULES)
    )

    # Root's tiers, seeded to match today's UI gating exactly.
    for tier_key, label, modules in (
        ('free', 'Free', FREE_MODULES),
        ('premium', 'Premium', PREMIUM_MODULES),
        ('org_owner', 'Org Owner', ORG_OWNER_MODULES),
    ):
        op.execute(
            """
            INSERT INTO organization_tiers (org_id, tier_key, label, modules)
            SELECT o.id, '%s', '%s', '%s'::jsonb
              FROM organizations o
             WHERE o.slug = 'root'
               AND NOT EXISTS (
                   SELECT 1 FROM organization_tiers t
                    WHERE t.org_id = o.id AND t.tier_key = '%s')
            """
            % (tier_key, label, _json_array(modules), tier_key)
        )

    # Every existing account becomes a root member.
    #   admin -> staff: org_owner tier, is_owner (they administer the platform)
    #   entitling subscription -> premium tier, held BY SUBSCRIPTION
    #   everyone else          -> free tier, granted
    #
    # The tier is derived from subscription_status, NOT from accounts.role,
    # even though role is normally kept in agreement with it by
    # role_for_subscription(). If a row has drifted (role='premium' with a
    # canceled subscription, e.g. a webhook that was missed), trusting role
    # here would hand that account tier='premium' with granted_by='grant' —
    # and grants are deliberately never touched by any webhook, so the lapsed
    # subscriber would keep premium forever. Deriving from the subscription
    # instead means the drift heals rather than becoming permanent.
    op.execute(
        """
        INSERT INTO organization_members
            (org_id, account_id, tier_key, granted_by, is_owner)
        SELECT
            o.id,
            a.id,
            CASE WHEN a.role = 'admin' THEN 'org_owner'
                 WHEN a.subscription_status IN ('active', 'trialing') THEN 'premium'
                 ELSE 'free' END,
            CASE WHEN a.role <> 'admin'
                  AND a.subscription_status IN ('active', 'trialing')
                 THEN 'subscription' ELSE 'grant' END,
            (a.role = 'admin')
          FROM accounts a
         CROSS JOIN organizations o
         WHERE o.slug = 'root'
           AND NOT EXISTS (
               SELECT 1 FROM organization_members m
                WHERE m.org_id = o.id AND m.account_id = a.id)
        """
    )

    op.execute(
        """
        UPDATE accounts
           SET default_org_id = (SELECT id FROM organizations WHERE slug = 'root')
         WHERE default_org_id IS NULL
        """
    )

    # All pre-existing sharing lives in root.
    for table in ('access_groups', 'access_grants', 'access_grant_events'):
        op.execute(
            f"""
            UPDATE {table}
               SET org_id = (SELECT id FROM organizations WHERE slug = 'root')
             WHERE org_id IS NULL
            """
        )


def downgrade() -> None:
    for table in ('access_grant_events', 'access_grants', 'access_groups'):
        op.drop_index(f'ix_{table}_org_id', table_name=table)
        op.drop_column(table, 'org_id')

    op.drop_index('ix_accounts_default_org_id', table_name='accounts')
    op.drop_column('accounts', 'default_org_id')

    op.drop_table('organization_members')
    op.drop_table('organization_tiers')
    op.drop_table('organizations')


def _json_array(values) -> str:
    """Render a Python list of module keys as a SQL-safe JSON array literal.

    The keys are developer-authored constants matching ^[a-z_]+$, never user
    input, but assert it anyway so a future edit cannot smuggle a quote into
    the raw SQL above.
    """
    for v in values:
        assert v.replace('_', '').isalnum(), f'unsafe module key: {v!r}'
    return '[' + ','.join(f'"{v}"' for v in values) + ']'
