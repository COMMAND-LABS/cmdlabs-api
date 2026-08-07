"""drop the organization slug

An org had an immutable public `slug`, and the one whose slug was 'root' was the
platform's own — the home of catalog content and the org staff had to be placed
in before they could work. Both jobs are gone: staff bypass the module ceiling
wherever they are, and publishing became a Space. The last thing the column did
was identify one special row.

What it cost while it existed: a permanent public name chosen at the worst
moment (before anyone knows how it will be used), a naming step in front of the
first invite, a reserved-word list, an availability endpoint, and `is_personal`
defined as `slug IS NULL` — which actually meant "not yet named" and was read
everywhere as "a workspace of one". Those came apart the moment anything could
join an unnamed org.

An id identifies an org in every route. `is_personal` is now a count of
members, which is the question it was always standing in for.

REVERSIBLE, BUT NOT FAITHFULLY. The downgrade regenerates 'org-{id}' rather
than inventing names: any name chosen here would be a permanent public
identifier nobody picked, which is the exact mistake the column existed to
avoid.

Revision ID: f4a5b6c7d8f0
Revises: e3f4a5b6c7d9
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f4a5b6c7d8f0'
down_revision: Union[str, None] = 'e3f4a5b6c7d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint('uq_organizations_slug', 'organizations', type_='unique')
    op.drop_column('organizations', 'slug')


def downgrade() -> None:
    op.add_column('organizations', sa.Column('slug', sa.String(64),
                                             nullable=True))
    # Only orgs that had been NAMED get one back. A workspace that never
    # claimed a slug had NULL before and gets NULL again — inventing one would
    # hand every signup a permanent public identity they never chose, which is
    # precisely what the original design refused to do.
    op.execute("""
        UPDATE organizations SET slug = 'org-' || id
         WHERE id IN (SELECT org_id FROM organization_members
                      GROUP BY org_id HAVING count(*) > 1)
    """)
    op.create_unique_constraint('uq_organizations_slug', 'organizations',
                                ['slug'])
