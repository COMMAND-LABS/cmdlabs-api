"""Drop organizations.data_scope

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-08-04 16:30:00.000000

CONTRACT half of the split. e3f4a5b6c7d8 gave every account its own org and set
every row to 'shared'; this removes the column and, with it, the branch in the
tenancy predicate.

Safe because the predicate collapses rather than changes. It was

    org_id == mine AND (data_scope == 'shared' OR created_by == me)

and after the split every org is 'shared', so the second clause is already
constant-true everywhere. Dropping it is a no-op on the result set — the check
below proves that against the live data before anything is removed.

What this buys is not tidiness. `data_scope` was the one place where the answer
to "can this account see this row?" depended on something other than org_id, and
it is the expression a reviewer has to hold in their head at all ~40 call sites.
Now there is one rule with no exceptions.

Reversible: downgrade re-adds the column defaulting to 'shared', which is what
every row held. It cannot restore the old root-as-lobby arrangement — that is
e3f4a5b6c7d8's downgrade, and the order matters.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a5b6c7d8e9'
down_revision: Union[str, None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Refuse if any org is still 'personal'. Dropping the column under one of
    # those would silently widen it from "each member sees their own rows" to
    # "every member sees everything" — the exact disclosure this whole design
    # exists to prevent, delivered by a migration that looked like cleanup.
    personal = conn.execute(sa.text(
        "SELECT id, slug, name FROM organizations WHERE data_scope <> 'shared'"
    )).fetchall()
    if personal:
        raise RuntimeError(
            f"These organizations are not 'shared': {personal}. Dropping "
            f"data_scope would widen visibility inside them. Run "
            f"e3f4a5b6c7d8 first, or move their members out."
        )

    op.drop_column('organizations', 'data_scope')


def downgrade() -> None:
    op.add_column('organizations', sa.Column(
        'data_scope', sa.String(20), nullable=False, server_default='shared'))
    op.create_check_constraint(
        'ck_org_data_scope', 'organizations',
        "data_scope IN ('personal','shared')")
