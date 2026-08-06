"""course plans and the platform catalog

Two changes, both about the same thing: letting somebody BROWSE what they have
not bought.

1. `courses.required_plan` — 'free' | 'premium'. Which plan opens a course.
   Defaults to 'free', so every course that already exists stays reachable by
   exactly the people who could reach it yesterday.

2. `visibility = 'catalog'` — a third arm alongside 'org' and 'granted'. A
   catalog course belongs to the PLATFORM org and is visible to every
   organization on the platform, gated by required_plan rather than by
   membership.

WHY 'catalog' IS NOT A HOLE IN THE TENANCY BOUNDARY
---------------------------------------------------
The same one-directional argument services/catalog.py already makes:

    Acme -> Beta       tenant data sideways      never
    platform -> Acme   our own courseware        fine

A catalog row may only ever live in the platform org — enforced in
routers/courses/crud.py, not merely intended here — so "Acme publishes a course
into Beta" cannot be expressed. The read arm can only ever add platform content
to a tenant's view, never another tenant's rows.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b6c7d8e9f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'courses',
        sa.Column('required_plan', sa.String(20), nullable=False,
                  server_default='free'),
    )
    op.create_check_constraint(
        'ck_courses_required_plan', 'courses',
        "required_plan IN ('free','premium')",
    )

    # Widened, not replaced: existing 'org' and 'granted' rows are untouched.
    op.drop_constraint('ck_courses_visibility', 'courses', type_='check')
    op.create_check_constraint(
        'ck_courses_visibility', 'courses',
        "visibility IN ('org','granted','catalog')",
    )

    # Let everyone who already signed up reach the catalog.
    #
    # `courses` joined both plans in config/plans_registry.py, but a ceiling is
    # written once at signup and only revisited when Stripe next says something
    # about that account. Without this, every existing account keeps a ceiling
    # from before the catalog existed and simply has no Courses menu item —
    # for free accounts, possibly forever, since nothing bills them.
    #
    # PERSONAL, BILLING-MANAGED orgs only. A team's ceiling is a staff decision
    # and a comped one ('grant') is a promise no automated statement may edit;
    # widening either here would hand out a module nobody chose to give.
    op.execute("""
        UPDATE organizations
           SET granted_modules = granted_modules || '["courses"]'::jsonb
         WHERE slug IS NULL
           AND ceiling_managed_by = 'subscription'
           AND NOT (granted_modules @> '["courses"]'::jsonb)
    """)


def downgrade() -> None:
    # The `courses` backfill is deliberately NOT reversed. A ceiling that has
    # been widened cannot be told apart from one an owner was always meant to
    # have, and stripping a module from every personal workspace to undo a
    # schema change is the more damaging of the two mistakes.

    # Catalog rows have no meaning under the old constraint — they are platform
    # content, not any tenant's enablement — so they go rather than being
    # rewritten into somebody's org as if an owner had enabled them.
    op.execute("DELETE FROM courses WHERE visibility = 'catalog'")
    op.drop_constraint('ck_courses_visibility', 'courses', type_='check')
    op.create_check_constraint(
        'ck_courses_visibility', 'courses',
        "visibility IN ('org','granted')",
    )
    op.drop_constraint('ck_courses_required_plan', 'courses', type_='check')
    op.drop_column('courses', 'required_plan')
