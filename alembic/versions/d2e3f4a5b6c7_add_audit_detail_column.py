"""Add a detail column to the audit log

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-08-04 16:00:00.000000

Widening the log to cover ceiling and tier changes meant recording WHAT
something became — a module list — and the only free-text-ish column available
was `role`, a String(20) meaning "the role involved in a grant".

That was a workaround, and it failed on the first realistic input:
"agent_chat,knowledge_bases,home" is 31 characters. Overloading a column with
a different meaning and a size chosen for that meaning was the actual mistake;
truncating or widening `role` would have preserved it.

`detail` is Text and unbounded, so an event can say what changed to what
instead of merely that something changed. `role` goes back to meaning only
what it always did.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('access_grant_events', sa.Column('detail', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('access_grant_events', 'detail')
