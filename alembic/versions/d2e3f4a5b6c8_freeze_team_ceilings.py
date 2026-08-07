"""freeze the ceilings of orgs that are already teams

The ceiling is now DERIVED for orgs whose ceiling_managed_by is 'subscription':
it reads the owner's plan rather than a stored copy, so adding a module to a
plan reaches everybody with no backfill.

That is right for a one-person workspace, where the owner IS the org. It is
wrong for a team: the people it would move are no longer the person paying, and
a colleague should not lose Contacts because the founder's card expired. Going
forward, letting somebody in freezes the ceiling (services.organizations.
freeze_ceiling, called from the invite path).

Orgs that were ALREADY teams never passed through that gate, so they are frozen
here — at exactly what they resolve to today, which is what they have been
using. Without this, the first Stripe event for an owner would silently move a
whole team's entitlement.

Revision ID: d2e3f4a5b6c8
Revises: c1d2e3f4a5b7
"""
from typing import Sequence, Union

from alembic import op

revision: str = 'd2e3f4a5b6c8'
down_revision: Union[str, None] = 'c1d2e3f4a5b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Freeze at the STORED value rather than recomputing from the owner's plan.
    # These orgs have been running on that column; recomputing could narrow a
    # team's access at migration time, which is the one outcome worth avoiding.
    op.execute("""
        UPDATE organizations
           SET ceiling_managed_by = 'grant'
         WHERE ceiling_managed_by = 'subscription'
           AND id IN (
                 SELECT org_id FROM organization_members
                 GROUP BY org_id HAVING count(*) > 1
           )
    """)


def downgrade() -> None:
    # Not reversed: a frozen ceiling is indistinguishable from one staff set by
    # hand, and handing team ceilings back to an owner's subscription is the
    # more damaging direction to guess in.
    pass
