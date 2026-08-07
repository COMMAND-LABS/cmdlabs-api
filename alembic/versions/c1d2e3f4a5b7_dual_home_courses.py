"""dual-home courses: a course lives in an org OR a space

THE INVARIANT THIS ADDS
-----------------------
    CHECK ((org_id IS NULL) <> (space_id IS NULL))

Exactly one home, enforced by the database rather than by discipline. A course
in an org is opened by that org's members; a course in a space is opened by
that space's members, whatever org they belong to. Container membership IS the
grant — a space course needs no visibility rule of its own.

The reason for the constraint is not tidiness. If a row could sit in both, "who
can see this?" would become a join across two membership tables with no single
answer, and the property that makes access here auditable would be gone.

Every existing course keeps org_id and is untouched.

ALSO: drops spaces.slug. It was added by symmetry with organizations, which
have since dropped theirs — an id already identifies a space in every route,
and a permanent public name carries squatting and link-stability consequences
that nothing needs yet. Cheap to add later; impossible to withdraw once links
point at it.

Revision ID: c1d2e3f4a5b7
Revises: f0a1b2c3d4e5
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c1d2e3f4a5b7'
down_revision: Union[str, None] = 'f0a1b2c3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('courses', sa.Column('space_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_courses_space_id', 'courses', 'spaces',
                          ['space_id'], ['id'], ondelete='CASCADE')
    op.create_index('ix_courses_space_id', 'courses', ['space_id'])

    # Nullable only now that a second home exists. Nothing is backfilled: every
    # existing row already has an org and keeps it.
    op.alter_column('courses', 'org_id', existing_type=sa.Integer(),
                    nullable=True)

    op.create_check_constraint(
        'ck_courses_one_home', 'courses',
        '(org_id IS NULL) <> (space_id IS NULL)',
    )
    # Postgres treats NULLs as distinct, so this constrains only the rows that
    # actually live in a space, exactly as uq_course_org_key does for orgs.
    op.create_unique_constraint('uq_course_space_key', 'courses',
                                ['space_id', 'course_key'])

    op.drop_constraint('uq_spaces_slug', 'spaces', type_='unique')
    op.drop_column('spaces', 'slug')


def downgrade() -> None:
    # Space courses cannot be expressed without space_id and are not anybody's
    # org enablement — rewriting them into the owning org would hand a tenant
    # content its members were never granted.
    op.execute("DELETE FROM courses WHERE space_id IS NOT NULL")

    op.drop_constraint('uq_course_space_key', 'courses', type_='unique')
    op.drop_constraint('ck_courses_one_home', 'courses', type_='check')
    op.alter_column('courses', 'org_id', existing_type=sa.Integer(),
                    nullable=False)
    op.drop_index('ix_courses_space_id', table_name='courses')
    op.drop_constraint('fk_courses_space_id', 'courses', type_='foreignkey')
    op.drop_column('courses', 'space_id')

    # Slugs are regenerated from the id rather than invented: any name chosen
    # here would be a permanent public identifier nobody picked.
    op.add_column('spaces', sa.Column('slug', sa.String(64), nullable=True))
    op.execute("UPDATE spaces SET slug = 'space-' || id WHERE slug IS NULL")
    op.alter_column('spaces', 'slug', existing_type=sa.String(64),
                    nullable=False)
    op.create_unique_constraint('uq_spaces_slug', 'spaces', ['slug'])
