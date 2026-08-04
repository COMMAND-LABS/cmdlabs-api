"""Add org_id to tenant tables, resource visibility, and the publishing catalog

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-08-04 12:00:00.000000

EXPAND half of an expand/migrate/contract. Columns are added NULLABLE and
backfilled; nothing reads them yet, and reads still filter on account_id. A
separate migration tightens them to NOT NULL once zero nulls is asserted, so a
failed constraint cannot roll back the backfill.

Every existing row belongs to the root org, because migration e8f9a0b1c2d3 made
every account a root member. That makes this backfill deterministic — there is
no row whose tenant has to be guessed.

Root is data_scope='personal', so once reads flip, the scoping clause

    org_id == root AND (shared OR created_by == me)

reduces to `created_by == me` — exactly today's behaviour. The read-flip is a
provable no-op, which is the whole reason for this ordering.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9a0b1c2d3e4'
down_revision: Union[str, None] = 'e8f9a0b1c2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The CRM cluster — all of it chains through `contacts`.
CRM_TABLES = [
    'contacts',
    'companies',
    'company_contacts',
    'contact_lists',
    'contact_list_members',
    'contact_events',
    'career_timeline',
    'deals',
]

# Publishable resources. These moved into this migration (rather than a later
# one) because a lesson has to BELONG to a client org for an intra-org grant to
# reach it — sharing from the platform's own org would be a cross-org grant,
# which the tenancy boundary forbids.
RESOURCE_TABLES = ['agents', 'vector_stores']

ORG_SCOPED_TABLES = CRM_TABLES + RESOURCE_TABLES

# vector_stores names its owner column differently from everything else.
OWNER_COLUMN = {'vector_stores': 'owner_account_id'}


def upgrade() -> None:
    # ------------------------------------------------------------- org_id
    for table in ORG_SCOPED_TABLES:
        op.add_column(table, sa.Column(
            'org_id', sa.Integer(),
            sa.ForeignKey('organizations.id', ondelete='CASCADE'),
            nullable=True,
        ))
        op.create_index(f'ix_{table}_org_id', table, ['org_id'])

    # ------------------------------------------------- intra-org visibility
    # 'private' = only the creator (plus anyone granted it explicitly);
    # 'org'     = every member of the org.
    #
    # Defaults to 'private' so the flip preserves today's behaviour exactly:
    # an agent is currently reachable by its owner and its grantees, and
    # nothing more. Fails closed — widening is always a deliberate act.
    for table in RESOURCE_TABLES:
        op.add_column(table, sa.Column(
            'visibility', sa.String(20), nullable=False, server_default='private',
        ))
        op.create_check_constraint(
            f'ck_{table}_visibility', table, "visibility IN ('private','org')",
        )

    # ------------------------------------------------------------- catalog
    # Publishing, not sharing. A lesson authored in the platform org is
    # published once and granted to many client orgs, so there is ONE live
    # version rather than a copy per client.
    #
    # This does not weaken the tenancy boundary, because direction is what
    # matters: platform -> tenant is publishing; tenant -> tenant would be a
    # leak. A catalog_item may only ever reference a resource owned by the
    # platform org, so "Acme publishes to Beta" is not expressible here.
    op.create_table(
        'catalog_items',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('resource_type', sa.String(20), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        # Publishing is a SEPARATE act from authoring, so nothing leaves the
        # platform org by accident.
        sa.Column('published_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('published_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
        sa.UniqueConstraint('resource_type', 'resource_id',
                            name='uq_catalog_item_resource'),
        sa.CheckConstraint("resource_type IN ('agent','vector_store')",
                           name='ck_catalog_item_resource_type'),
    )

    op.create_table(
        'catalog_grants',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('catalog_item_id', sa.Integer(),
                  sa.ForeignKey('catalog_items.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        # NULL = the whole org. Set = only that department.
        sa.Column('group_id', sa.Integer(),
                  sa.ForeignKey('access_groups.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('granted_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('granted_by_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'), nullable=True),
    )

    # Two indexes rather than one UniqueConstraint: Postgres treats NULLs as
    # distinct, so UNIQUE(item, org, group) would happily allow two identical
    # whole-org grants. The partial index covers that case explicitly.
    op.create_index(
        'uq_catalog_grant_group', 'catalog_grants',
        ['catalog_item_id', 'org_id', 'group_id'],
        unique=True, postgresql_where=sa.text('group_id IS NOT NULL'),
    )
    op.create_index(
        'uq_catalog_grant_org', 'catalog_grants',
        ['catalog_item_id', 'org_id'],
        unique=True, postgresql_where=sa.text('group_id IS NULL'),
    )

    # ------------------------------------------------------------ backfill
    # Idempotent. Every row belongs to root, since every account is a root
    # member — no row's tenant has to be inferred.
    for table in ORG_SCOPED_TABLES:
        owner_col = OWNER_COLUMN.get(table, 'account_id')
        op.execute(
            f"""
            UPDATE {table} t
               SET org_id = a.default_org_id
              FROM accounts a
             WHERE a.id = t.{owner_col}
               AND t.org_id IS NULL
               AND a.default_org_id IS NOT NULL
            """
        )
        # Rows whose owning account has since been deleted (or predates the
        # membership backfill) still need a home, or the NOT NULL step in the
        # next migration would fail on them.
        op.execute(
            f"""
            UPDATE {table}
               SET org_id = (SELECT id FROM organizations WHERE slug = 'root')
             WHERE org_id IS NULL
            """
        )


def downgrade() -> None:
    op.drop_index('uq_catalog_grant_org', table_name='catalog_grants')
    op.drop_index('uq_catalog_grant_group', table_name='catalog_grants')
    op.drop_table('catalog_grants')
    op.drop_table('catalog_items')

    for table in RESOURCE_TABLES:
        op.drop_constraint(f'ck_{table}_visibility', table, type_='check')
        op.drop_column(table, 'visibility')

    for table in ORG_SCOPED_TABLES:
        op.drop_index(f'ix_{table}_org_id', table_name=table)
        op.drop_column(table, 'org_id')
