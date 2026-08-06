"""Add courses: per-org enablement of code-backed course experiences

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-08-04 19:00:00.000000

A course is a dynamic experience that lives in the Next.js router — components,
embedded agents, interactive steps. The CONTENT is code, so it is not stored
here and never will be. What this table stores is a per-organization
ENABLEMENT: "this org may open the course whose stable key is `bsop-intro`",
plus how it is titled and ordered for them.

That split is what keeps tenancy intact. Because the content is platform code
rather than tenant data, two orgs holding the same course_key render the same
route — the same way both render /dashboard/contacts. Nothing moves between
tenants, so there is nothing to leak, and no publishing mechanism is needed:
one copy exists by construction.

`course_key` is a STABLE IDENTIFIER, not a title. Renaming the route folder in
the UI must never revoke anybody's access, which is the same rule
modules_registry.py exists to enforce. The key is validated for shape here; the
UI owns which keys actually have routes, because that is where the content is.

Access rides entirely on machinery that already exists:
  - everyone in the org sees the org's courses (tenant_predicate)
  - a department sees specific ones (access_grants, principal_type='group')
Both are already org-confined and validated by access.assert_same_org, so this
migration adds a table and widens one CHECK constraint. Nothing else.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b6c7d8e9f0a1'
down_revision: Union[str, None] = 'a5b6c7d8e9f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


GRANT_RESOURCE_TYPES = ['agent', 'vector_store', 'credential', 'course']
PREVIOUS_RESOURCE_TYPES = ['agent', 'vector_store', 'credential']


def upgrade() -> None:
    op.create_table(
        'courses',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        # The tenant. A course enablement belongs to exactly one org, like
        # every other scoped row.
        sa.Column('org_id', sa.Integer(),
                  sa.ForeignKey('organizations.id', ondelete='CASCADE'),
                  nullable=False, index=True),
        # Stable identifier matching a route in the UI. Never a display name.
        sa.Column('course_key', sa.String(64), nullable=False),
        sa.Column('title', sa.String(255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        # 'org'    = every member of this org may open it
        # 'granted' = only accounts/groups holding an AccessGrant on it
        #
        # Defaults to 'org' because the common case is "this client's whole
        # team takes this course", and the narrow case is the deliberate one.
        sa.Column('visibility', sa.String(20), nullable=False,
                  server_default='org'),
        # Attribution, never tenancy — same as every other table since org
        # scoping landed.
        sa.Column('account_id', sa.Integer(),
                  sa.ForeignKey('accounts.id', ondelete='SET NULL'),
                  nullable=True, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        # One enablement per course per org. Enabling twice is a no-op rather
        # than a duplicate row that would render the course twice in a list.
        sa.UniqueConstraint('org_id', 'course_key', name='uq_course_org_key'),
        sa.CheckConstraint("visibility IN ('org','granted')",
                           name='ck_courses_visibility'),
    )

    # Grants can now name a course. The constraint is what stops a typo in
    # resource_type from creating a grant that silently resolves to nothing.
    op.drop_constraint('ck_access_grant_resource_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_resource_type', 'access_grants',
        "resource_type IN (" + ", ".join(f"'{t}'" for t in GRANT_RESOURCE_TYPES) + ")")


def downgrade() -> None:
    # Course grants would violate the narrower constraint, and a grant row is a
    # record of access having been given. Remove them explicitly rather than
    # letting the ALTER fail halfway.
    op.execute("DELETE FROM access_grants WHERE resource_type = 'course'")
    op.drop_constraint('ck_access_grant_resource_type', 'access_grants',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_resource_type', 'access_grants',
        "resource_type IN (" + ", ".join(f"'{t}'" for t in PREVIOUS_RESOURCE_TYPES) + ")")

    op.drop_table('courses')
