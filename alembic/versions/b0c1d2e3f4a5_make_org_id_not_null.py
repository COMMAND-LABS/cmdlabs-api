"""Make org_id NOT NULL on the tenant tables

Revision ID: b0c1d2e3f4a5
Revises: f9a0b1c2d3e4
Create Date: 2026-08-04 13:00:00.000000

CONTRACT half of expand/migrate/contract.

Deliberately a SEPARATE migration from the backfill (f9a0b1c2d3e4): if the
constraint fails on some row, only this migration rolls back, leaving the
backfill in place to inspect. Combined, a failure here would undo the backfill
too and you would have to redo it blind.

ORDERING MATTERS. This must land AFTER every write path sets org_id, not
before. org_id has no server default, so constraining it while a create
endpoint still omits the column would break every insert on these ten tables.
The write paths were updated first, and tests/test_org_write_scoping.py pins
that they stay that way.

Preflight below refuses to run rather than failing partway through, so the
error names the offending table instead of surfacing as a constraint
violation from somewhere inside a ten-table loop.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b0c1d2e3f4a5'
down_revision: Union[str, None] = 'f9a0b1c2d3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ORG_SCOPED_TABLES = [
    'contacts',
    'companies',
    'company_contacts',
    'contact_lists',
    'contact_list_members',
    'contact_events',
    'career_timeline',
    'deals',
    'agents',
    'vector_stores',
]


def upgrade() -> None:
    bind = op.get_bind()

    # Preflight. A NULL here means the backfill missed rows — most likely rows
    # written between the backfill and this deploy by a code path that does not
    # set org_id. Constraining anyway would fail mid-loop with a message that
    # names the constraint but not the cause.
    offenders = []
    for table in ORG_SCOPED_TABLES:
        nulls = bind.execute(
            sa.text(f"SELECT count(*) FROM {table} WHERE org_id IS NULL")
        ).scalar()
        if nulls:
            offenders.append(f"{table}: {nulls} row(s)")

    if offenders:
        raise RuntimeError(
            "Refusing to add NOT NULL — org_id is still NULL on:\n  "
            + "\n  ".join(offenders)
            + "\n\nBackfill those rows first. Every row must belong to an org; "
              "a row with no org is one no tenant can ever see."
        )

    for table in ORG_SCOPED_TABLES:
        op.alter_column(table, 'org_id', existing_type=sa.Integer(), nullable=False)


def downgrade() -> None:
    for table in ORG_SCOPED_TABLES:
        op.alter_column(table, 'org_id', existing_type=sa.Integer(), nullable=True)
