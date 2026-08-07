"""the catalog becomes a space

WHAT THE CATALOG WAS
--------------------
Two tables — catalog_items and catalog_grants — expressing "the PLATFORM
publishes a resource, granted to an org and optionally narrowed to one of its
access groups". A space is the same idea with the special case removed: any
account may share a resource they own, and the audience is the space's members.

    catalog_items + catalog_grants   ->   space_resources
    "the platform org may publish"   ->   "whoever owns it may share it"
    "granted to an org, maybe a group" -> "the members of a space"

One table instead of two, one membership question instead of three, and no org
has to be special for publishing to work — which is what let the platform org
stop being a special row at all.

WHY THE LIVE ROWS ARE MIGRATED AND NOT DROPPED
----------------------------------------------
There were two, both reaching real people: an agent and its knowledge base,
granted to an access group inside a client org. Dropping the tables without
moving them would have quietly revoked four accounts' access to the thing they
use. They are moved into a space whose membership is exactly the accounts that
could reach them before, so nobody gains and nobody loses.

Revision ID: e3f4a5b6c7d9
Revises: d2e3f4a5b6c8
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'e3f4a5b6c7d9'
down_revision: Union[str, None] = 'd2e3f4a5b6c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
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
        sa.CheckConstraint("resource_type IN ('agent','vector_store')",
                           name='ck_space_resource_type'),
    )
    op.create_index('ix_space_resources_space_id', 'space_resources',
                    ['space_id'])

    # ── move whatever the catalog was actually serving ──────────────────────
    #
    # One space per (published item -> audience) pairing would fragment a
    # coherent bundle, so this makes ONE space per granted org+group and puts
    # every item that audience could reach into it. That is what the audience
    # experienced: a set of things they could open.
    conn = op.get_bind()
    pairings = conn.execute(sa.text("""
        SELECT DISTINCT g.org_id, g.group_id
          FROM catalog_grants g
    """)).fetchall()

    for org_id, group_id in pairings:
        label = conn.execute(sa.text("""
            SELECT name FROM access_groups WHERE id = :gid
        """), {"gid": group_id}).scalar() if group_id else None
        if label is None:
            label = conn.execute(sa.text("""
                SELECT name FROM organizations WHERE id = :oid
            """), {"oid": org_id}).scalar() or f"Org {org_id}"

        # Owned by whoever published the content, billed to their org. The
        # first item's publisher stands in for "the platform" now that the
        # platform is not a special row.
        publisher = conn.execute(sa.text("""
            SELECT i.published_by_account_id
              FROM catalog_grants g
              JOIN catalog_items i ON i.id = g.catalog_item_id
             WHERE g.org_id = :oid AND g.group_id IS NOT DISTINCT FROM :gid
             ORDER BY i.id LIMIT 1
        """), {"oid": org_id, "gid": group_id}).scalar()

        publisher_org = conn.execute(sa.text("""
            SELECT default_org_id FROM accounts WHERE id = :aid
        """), {"aid": publisher}).scalar() if publisher else None

        space_id = conn.execute(sa.text("""
            INSERT INTO spaces (name, description, owner_account_id,
                                owner_org_id, discoverable, join_policy, status)
            VALUES (:name, :descr, :owner, :owner_org, false, 'invite', 'active')
            RETURNING id
        """), {
            "name": label,
            "descr": "Migrated from the platform catalog.",
            "owner": publisher,
            "owner_org": publisher_org,
        }).scalar()

        # Seeded tiers, matching what services.spaces.create_space would do.
        for key, tier_label in (("owner", "Owner"), ("member", "Member")):
            conn.execute(sa.text("""
                INSERT INTO space_tiers (space_id, tier_key, label)
                VALUES (:sid, :key, :label)
            """), {"sid": space_id, "key": key, "label": tier_label})

        # The resources that audience could reach.
        conn.execute(sa.text("""
            INSERT INTO space_resources (space_id, resource_type, resource_id,
                                         added_by_account_id)
            SELECT DISTINCT :sid, i.resource_type, i.resource_id,
                   i.published_by_account_id
              FROM catalog_grants g
              JOIN catalog_items i ON i.id = g.catalog_item_id
             WHERE g.org_id = :oid AND g.group_id IS NOT DISTINCT FROM :gid
        """), {"sid": space_id, "oid": org_id, "gid": group_id})

        # Exactly the accounts that could reach them before: the group's
        # members if the grant was group-scoped, otherwise the whole org.
        if group_id is not None:
            conn.execute(sa.text("""
                INSERT INTO space_members (space_id, account_id, tier_key,
                                           is_owner, granted_by)
                SELECT :sid, m.account_id, 'member', false, 'grant'
                  FROM access_group_members m
                 WHERE m.access_group_id = :gid
                ON CONFLICT (space_id, account_id) DO NOTHING
            """), {"sid": space_id, "gid": group_id})
        else:
            conn.execute(sa.text("""
                INSERT INTO space_members (space_id, account_id, tier_key,
                                           is_owner, granted_by)
                SELECT :sid, m.account_id, 'member', false, 'grant'
                  FROM organization_members m
                 WHERE m.org_id = :oid
                ON CONFLICT (space_id, account_id) DO NOTHING
            """), {"sid": space_id, "oid": org_id})

        # The publisher owns it — a space with no owner cannot be administered.
        if publisher is not None:
            conn.execute(sa.text("""
                INSERT INTO space_members (space_id, account_id, tier_key,
                                           is_owner, granted_by)
                VALUES (:sid, :aid, 'owner', true, 'grant')
                ON CONFLICT (space_id, account_id)
                DO UPDATE SET is_owner = true, tier_key = 'owner'
            """), {"sid": space_id, "aid": publisher})

    op.drop_table('catalog_grants')
    op.drop_table('catalog_items')


def downgrade() -> None:
    op.create_table(
        'catalog_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('resource_type', sa.String(20), nullable=False),
        sa.Column('resource_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('published_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('published_by_account_id', sa.Integer(), nullable=True),
    )
    op.create_table(
        'catalog_grants',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('catalog_item_id', sa.Integer(),
                  sa.ForeignKey('catalog_items.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=True),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.func.now()),
        sa.Column('granted_by_account_id', sa.Integer(), nullable=True),
    )
    # The spaces are NOT unwound. A space may have gained members and resources
    # since the migration, and rebuilding catalog rows from it would invent
    # grants nobody made. The tables come back empty; re-publishing is a
    # deliberate act.
    op.drop_index('ix_space_resources_space_id', table_name='space_resources')
    op.drop_table('space_resources')
