"""ownership lives in one place

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6

OWNERSHIP WAS STORED TWICE.

    organizations.owner_account_id        who owns the org
    organization_members.is_owner         ...also who owns the org

Both were load-bearing and nothing kept them in step: `owner_account_id`
decides whose subscription funds the org (services/modules.org_entitlement),
`is_owner` decides who may administer it (_require_owner) and grants the module
bypass. A stored copy of a derivable value is a cache with no invalidation, and
this one had already drifted — organizations whose owner_account_id named an
account that held no is_owner row, and so could not open the org it owned.

deps.get_org_context now derives it (`org.owner_account_id == account_id`) off
the Organization row it has already joined for the membership check. Same
answer, one source, no way for the two to disagree because there is only one.

BEFORE DROPPING ANYTHING, THIS MIGRATION CHECKS. If any surviving is_owner row
disagrees with its org's owner_account_id, the upgrade ABORTS rather than
silently picking a winner — because picking wrong in either direction is either
locking an owner out of their org or leaving somebody with administrative
access they were never given. A disagreement means somebody has to look.

Two shapes are NOT disagreements and are handled rather than refused:
  - an is_owner row for an account that is not the org's owner_account_id
    where owner_account_id IS NULL. Nobody is named, so the flag is the only
    record; the column is adopted from the flag.
  - an owner_account_id with no is_owner membership row. That is the drift this
    change exists to end, and it needs no data: after this migration the owner
    simply is the owner, member row or not, and joining is the separate act it
    always was.

Create Date: 2026-08-08
"""
from alembic import op
import sqlalchemy as sa


revision = 'a2b3c4d5e6f7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    # Adopt: an org with no named owner, but exactly one member flagged as one.
    adopted = conn.execute(sa.text("""
        UPDATE organizations o
           SET owner_account_id = m.account_id
          FROM organization_members m
         WHERE m.org_id = o.id
           AND m.is_owner
           AND o.owner_account_id IS NULL
           AND (SELECT count(*) FROM organization_members x
                 WHERE x.org_id = o.id AND x.is_owner) = 1
    """)).rowcount

    # Refuse: a flag naming somebody the org does not call its owner.
    conflicts = conn.execute(sa.text("""
        SELECT o.id, o.name, o.owner_account_id, m.account_id
          FROM organizations o
          JOIN organization_members m ON m.org_id = o.id
         WHERE m.is_owner
           AND o.owner_account_id IS DISTINCT FROM m.account_id
         ORDER BY o.id
    """)).fetchall()
    if conflicts:
        detail = "; ".join(
            f"org {r[0]} ({r[1]!r}) names owner {r[2]} but account {r[3]} "
            f"holds is_owner" for r in conflicts)
        raise RuntimeError(
            "Refusing to collapse ownership: "
            f"{len(conflicts)} organization(s) disagree with themselves. "
            "Resolve each by hand — picking a winner automatically would "
            "either lock an owner out or hand somebody administrative access "
            f"they were not given. {detail}")

    orphans = conn.execute(sa.text("""
        SELECT count(*) FROM organizations o
         WHERE o.owner_account_id IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM organization_members m
                            WHERE m.org_id = o.id
                              AND m.account_id = o.owner_account_id)
    """)).scalar()

    print(f"[ownership] {adopted} org(s) adopted an owner from the flag; "
          f"0 conflicts; {orphans} owner(s) not currently a member of their "
          f"own org (unchanged — they remain the owner)")

    op.drop_column('organization_members', 'is_owner')


def downgrade():
    """Restores the column and rebuilds it from the one remaining source.

    Lossless in the only direction that matters: every org names its owner, so
    the flag can be reconstructed exactly. What cannot come back is a
    disagreement between the two — which is the point, and not something worth
    preserving.
    """
    op.add_column('organization_members',
                  sa.Column('is_owner', sa.Boolean(), nullable=False,
                            server_default=sa.text('false')))
    op.execute("""
        UPDATE organization_members m
           SET is_owner = true
          FROM organizations o
         WHERE o.id = m.org_id
           AND o.owner_account_id = m.account_id
    """)
