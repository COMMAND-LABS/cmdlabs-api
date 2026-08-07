"""backfill the spaces module into existing ceilings

`spaces` joined both plans in config/plans_registry.py when the second
container shipped, but a ceiling is written once at signup and only revisited
when Stripe next says something about that account. So every account that
existed before Spaces has a ceiling that predates the module and simply has no
Spaces menu item — for free accounts possibly forever, since nothing bills them
and nothing triggers a resync.

Exactly the same omission the courses backfill in c7d8e9f0a1b2 fixed, missed
here because the module was added to the plans AFTER that migration was
written. Kept as its own revision rather than folded into d8e9f0a1b2c3, which
has already been committed.

PERSONAL, BILLING-MANAGED orgs only. A team's ceiling is a staff decision and a
comped one ('grant') is a promise no automated statement may edit; widening
either would hand out a module nobody chose to give.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'e9f0a1b2c3d4'
down_revision: Union[str, None] = 'd8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        UPDATE organizations
           SET granted_modules = granted_modules || '["spaces"]'::jsonb
         WHERE slug IS NULL
           AND ceiling_managed_by = 'subscription'
           AND NOT (granted_modules @> '["spaces"]'::jsonb)
    """)


def downgrade() -> None:
    # Not reversed, for the reason c7d8e9f0a1b2 gives: a widened ceiling cannot
    # be told apart from one an owner was always meant to have, and stripping a
    # module from every personal workspace to undo a data fix is the more
    # damaging of the two mistakes.
    pass
