"""one tier vocabulary

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7

TWO GENERATIONS OF TIER NAMES WERE LIVING IN THE SAME TABLE.

    owner, member                 what the code seeds now       276 orgs each
    free, premium, org_owner      the original 2024 migration   1 org

The old three were never migrated, so exactly one organization — the platform
org — still carried them, and it is the one anybody browsing the admin UI looks
at first. Worse, two of the three borrowed the PLAN axis's vocabulary: an org
could be on the `premium` PLAN while a member held the `premium` TIER, meaning
two unrelated things one word apart.

`org_owner` was the third, and it named OWNERSHIP — which lives in
organizations.owner_account_id and is not a tier. An owner bypasses their tier
entirely (services/modules.effective_modules), so a member holding `org_owner`
got the org's whole ceiling and the tier's own module list was decoration. The
admin page showed both at once: "Org Owner — 6 modules" beside a member with 17.

WHAT MOVES. Any member still on one of the three retired keys is moved to a
surviving tier — `owner` if the org names them its owner, `member` otherwise —
and the three tier rows are deleted. Assigning by ownership rather than by the
old key's name is deliberate: `org_owner` meant "the owner" and `free`/`premium`
meant a plan, so neither carries information about which SET of modules the
person should now have. Ownership does.

NOBODY LOSES ACCESS. An owner bypasses tiers, so the owner's row is cosmetic
either way. A non-owner moving to `member` gets that org's member tier, which is
what every org seeded after e3f4a5b6c7d8 gives its non-owners.

Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = 'b3c4d5e6f7a8'
down_revision = 'a2b3c4d5e6f7'
branch_labels = None
depends_on = None

RETIRED = ('free', 'premium', 'org_owner')


def upgrade():
    conn = op.get_bind()

    # Seed the surviving vocabulary anywhere it is missing, BEFORE moving
    # anybody onto it. Orgs created since e3f4a5b6c7d8 already have both; the
    # ones that predate it — the platform org among them — have only the three
    # retired keys, so moving a member to `owner` there would point them at a
    # tier that does not exist.
    #
    # `member` seeds EMPTY, matching services/organizations.ensure_org_tiers:
    # an invited person gets what the owner deliberately checks in the matrix,
    # never a default nobody chose. `owner` seeds empty too and it costs
    # nothing — an owner bypasses their tier and takes the org's whole ceiling.
    seeded = 0
    for key, label in (('owner', 'Owner'), ('member', 'Member')):
        seeded += conn.execute(sa.text("""
            INSERT INTO organization_tiers (org_id, tier_key, label, modules)
            SELECT o.id, :key, :label, '[]'::jsonb
              FROM organizations o
             WHERE NOT EXISTS (SELECT 1 FROM organization_tiers t
                                WHERE t.org_id = o.id AND t.tier_key = :key)
        """), {"key": key, "label": label}).rowcount

    # Members next: the tier rows they point at are about to go, and a member
    # left naming a deleted tier would resolve to no modules at all.
    moved = conn.execute(sa.text("""
        UPDATE organization_members m
           SET tier_key = CASE WHEN o.owner_account_id = m.account_id
                               THEN 'owner' ELSE 'member' END
          FROM organizations o
         WHERE o.id = m.org_id
           AND m.tier_key IN :retired
    """).bindparams(sa.bindparam("retired", RETIRED, expanding=True))).rowcount

    dropped = conn.execute(sa.text(
        "DELETE FROM organization_tiers WHERE tier_key IN :retired"
    ).bindparams(sa.bindparam("retired", RETIRED, expanding=True))).rowcount

    # Every org must still have somewhere for its members to point. Seeded
    # since e3f4a5b6c7d8, but asserted rather than assumed: this migration is
    # the last chance to notice before somebody's menu goes empty.
    stranded = conn.execute(sa.text("""
        SELECT count(*) FROM organization_members m
         WHERE NOT EXISTS (SELECT 1 FROM organization_tiers t
                            WHERE t.org_id = m.org_id
                              AND t.tier_key = m.tier_key)
    """)).scalar()
    if stranded:
        raise RuntimeError(
            f"Refusing to finish: {stranded} member(s) now name a tier their "
            "org does not have, which would resolve to an empty module set. "
            "Seed the missing 'owner'/'member' tiers and re-run.")

    print(f"[tiers] {seeded} surviving tier(s) seeded where missing; "
          f"{moved} member(s) moved off the retired vocabulary; "
          f"{dropped} tier row(s) deleted; 0 stranded")


def downgrade():
    """One-way on purpose.

    The retired rows can be recreated, but which members held them cannot: the
    move above deliberately reassigned by OWNERSHIP rather than by the old key,
    because the old keys did not describe a module set. Recreating empty tiers
    nobody holds would restore the shape of the problem without its data, which
    is worse than leaving it undone.
    """
    pass
