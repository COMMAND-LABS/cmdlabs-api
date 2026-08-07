"""access groups become spaces

Revision ID: a5b6c7d8e9f1
Revises: f4a5b6c7d8f0

ONE CONTAINER FOR PEOPLE, NOT TWO.

An access group was "a named set of accounts, owned by somebody, that a
resource can be shared with." A space is the same sentence. The only real
differences were that a group could not be discovered, could not be joined, and
could not hold content — three things a space can do, all of which a group
would eventually have wanted.

So the group becomes a PRIVATE space: discoverable=false, join_policy='invite'.
Nothing about it is browsable and nobody can ask to join; it behaves exactly as
the group did, and its owner can now open it up deliberately if they choose.

WHAT MOVES, AND WHAT DELIBERATELY DOES NOT
------------------------------------------
Every access_groups row becomes a space. Every access_group_members row becomes
a space_members row — EXCEPT rows for accounts that are no longer members of
the org the group belongs to.

That exception is the whole safety argument. A grant to a group was confined to
the group's org (access_grants.org_id, checked on both the read and the write
path), so an ex-colleague still sitting in a group row reached nothing at all —
the platform-admin UI has been rendering exactly those rows in amber with
"grants nothing" for that reason. Space membership is NOT org-confined, by
design. Migrating those rows verbatim would therefore hand access back to
people who had lost it, silently, as a side effect of a refactor. They are
dropped and counted instead.

`access_grants` must be empty of group rows before this runs. It is asserted
rather than assumed: converting a group grant into a space share would widen a
same-org grant onto the cross-org rail, and that is a decision to be made in
daylight, not inside a schema migration.

MATCHING RATHER THAN BLINDLY INSERTING
--------------------------------------
An earlier migration (e3f4a5b6c7d9, the catalog) already turned one group into
a space by hand. Matching on (owner_account_id, name) reuses that space and
tops up any missing members, so this migration is idempotent and does not leave
the platform with two spaces of the same name owned by the same person.

Create Date: 2026-08-06
"""
from alembic import op
import sqlalchemy as sa


revision = 'a5b6c7d8e9f1'
down_revision = 'f4a5b6c7d8f0'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    stray = conn.execute(sa.text(
        "SELECT count(*) FROM access_grants WHERE principal_type = 'group'"
    )).scalar()
    if stray:
        raise RuntimeError(
            f"{stray} access_grants rows still name a group. Convert or revoke "
            "them before running this migration — turning a same-org grant "
            "into a cross-org space share is not something a migration should "
            "decide on its own."
        )

    groups = conn.execute(sa.text(
        "SELECT id, name, owner_account_id, org_id FROM access_groups ORDER BY id"
    )).fetchall()

    migrated = reused = dropped = 0
    for group_id, name, owner_account_id, org_id in groups:
        space_id = conn.execute(sa.text("""
            SELECT id FROM spaces
             WHERE owner_account_id = :owner AND name = :name
             ORDER BY id LIMIT 1
        """), {"owner": owner_account_id, "name": name}).scalar()

        if space_id is None:
            space_id = conn.execute(sa.text("""
                INSERT INTO spaces (name, owner_account_id, owner_org_id,
                                    discoverable, join_policy, status, created_at)
                VALUES (:name, :owner, :org, false, 'invite', 'active', now())
                RETURNING id
            """), {"name": name, "owner": owner_account_id, "org": org_id}).scalar()
            # The same pair every space is seeded with. A group had no tiers,
            # so everyone lands on 'member' and the owner on 'owner'.
            for key, label in (("owner", "Owner"), ("member", "Member")):
                conn.execute(sa.text("""
                    INSERT INTO space_tiers (space_id, tier_key, label, created_at)
                    VALUES (:space, :key, :label, now())
                    ON CONFLICT DO NOTHING
                """), {"space": space_id, "key": key, "label": label})
            migrated += 1
        else:
            reused += 1

        if owner_account_id is not None:
            conn.execute(sa.text("""
                INSERT INTO space_members (space_id, account_id, tier_key,
                                           is_owner, granted_by, created_at)
                VALUES (:space, :account, 'owner', true, 'grant', now())
                ON CONFLICT (space_id, account_id) DO NOTHING
            """), {"space": space_id, "account": owner_account_id})

        # Only people the group actually reached. See the header.
        members = conn.execute(sa.text("""
            SELECT m.account_id
              FROM access_group_members m
             WHERE m.access_group_id = :group
               AND EXISTS (SELECT 1 FROM organization_members om
                            WHERE om.org_id = :org
                              AND om.account_id = m.account_id)
        """), {"group": group_id, "org": org_id}).fetchall()

        total = conn.execute(sa.text(
            "SELECT count(*) FROM access_group_members WHERE access_group_id = :g"
        ), {"g": group_id}).scalar()
        dropped += total - len(members)

        for (account_id,) in members:
            conn.execute(sa.text("""
                INSERT INTO space_members (space_id, account_id, tier_key,
                                           is_owner, granted_by, created_at)
                VALUES (:space, :account, 'member', false, 'grant', now())
                ON CONFLICT (space_id, account_id) DO NOTHING
            """), {"space": space_id, "account": account_id})

    print(f"[access-groups→spaces] {migrated} created, {reused} matched an "
          f"existing space, {dropped} stale memberships dropped")

    op.drop_table('access_group_members')
    op.drop_table('access_groups')

    # A principal is now always an account. The cross-org audience lives in
    # space_resources, where the org filter deliberately does not apply.
    op.drop_constraint('ck_access_grant_principal_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_principal_type', 'access_grants',
        "principal_type IN ('account')")


def downgrade():
    """Restores the SHAPE, never the rows.

    The spaces this migration created are left in place: deleting them would
    remove memberships people may have edited since, and a space is a superset
    of a group in every respect. Expect empty group tables.
    """
    op.drop_constraint('ck_access_grant_principal_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_principal_type', 'access_grants',
        "principal_type IN ('account','group')")

    op.create_table(
        'access_groups',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('name', sa.String(255), nullable=False),
        sa.Column('owner_account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=True, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        'access_group_members',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('access_group_id', sa.Integer(),
                  sa.ForeignKey('access_groups.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        sa.Column('role', sa.String(50), nullable=False,
                  server_default='member'),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('access_group_id', 'account_id',
                            name='uq_access_group_members_group_account'),
    )
