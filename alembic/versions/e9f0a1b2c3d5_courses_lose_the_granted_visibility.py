"""courses lose the 'granted' visibility

Revision ID: e9f0a1b2c3d5
Revises: d8e9f0a1b2c4

THREE WAYS TO REACH A COURSE BECOME TWO.

    'org'      the container's members open it        (kept)
    'granted'  only accounts named by an AccessGrant  (gone)
    'catalog'  every org, gated by required_plan      (kept)

The middle one was a per-course permission sitting on top of the membership
that had already decided who was in the container. Reaching a SUBSET of people
is what the second container is for: put the course in a space and invite
exactly those people. That is one mechanism instead of two, and unlike a grant
it works across organizations — which is what somebody narrowing a course to "a
department" usually turns out to want next.

WHAT HAPPENS TO EXISTING ROWS. A 'granted' course becomes 'org'. That WIDENS
it: everybody in the org can now open what previously only named accounts
could. The alternative — deleting the rows, or leaving them unreachable — would
take a course away from the people who could already open it, and a migration
that silently removes access is worse than one that says out loud it is adding
some. Both counts are printed either way.

This database has no courses at all and no course grants, so the loop below is
a no-op here; it exists for any environment that is not this one.

`course` also leaves the access_grants resource_type CHECK. A grant names a
RESOURCE — something carrying credentials, quotas or content of its own. A
course is an enablement, and it now has exactly one thing deciding who opens
it.

Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'e9f0a1b2c3d5'
down_revision = 'd8e9f0a1b2c4'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    widened = conn.execute(sa.text(
        "UPDATE courses SET visibility = 'org' WHERE visibility = 'granted'"
    )).rowcount
    dropped = conn.execute(sa.text(
        "DELETE FROM access_grants WHERE resource_type = 'course'"
    )).rowcount
    print(f"[courses] {widened} 'granted' course(s) widened to the whole org; "
          f"{dropped} course grant(s) removed")

    op.drop_constraint('ck_courses_visibility', 'courses', type_='check')
    op.create_check_constraint(
        'ck_courses_visibility', 'courses',
        "visibility IN ('org','catalog')")

    op.drop_constraint('ck_access_grant_resource_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_resource_type', 'access_grants',
        "resource_type IN ('agent','vector_store','credential')")


def downgrade():
    """Restores the vocabulary, never the grants.

    Which accounts a 'granted' course reached is gone — those AccessGrant rows
    were deleted above, and the courses they applied to are now plain org
    courses. Widening back to 'granted' without them would hide a course from
    everybody, so nothing is reclassified here: the values become legal again
    and every row stays where it is.
    """
    op.drop_constraint('ck_courses_visibility', 'courses', type_='check')
    op.create_check_constraint(
        'ck_courses_visibility', 'courses',
        "visibility IN ('org','granted','catalog')")

    op.drop_constraint('ck_access_grant_resource_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_resource_type', 'access_grants',
        "resource_type IN ('agent','vector_store','credential','course')")
