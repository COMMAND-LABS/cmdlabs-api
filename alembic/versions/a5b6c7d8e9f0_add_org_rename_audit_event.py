"""Add the org.rename audit event type

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-08-04 18:00:00.000000

Renaming an organization changes what everybody in it sees at the top of the
switcher, and what its name reads as in every audit entry written afterwards.
The log snapshots labels at write time precisely so history survives a rename —
which only works if the rename itself is in the log, or the record shows a name
changing with nothing explaining when or by whom.

Small on its own. Included as a migration rather than squeezed into an existing
event type because the CHECK constraint is the thing that keeps the vocabulary
honest; widening it deliberately is cheaper than one more overloaded verb.
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a5b6c7d8e9f0'
down_revision: Union[str, None] = 'f4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in step with services/audit.py. Copied rather than imported: a migration
# is a point-in-time snapshot and must not follow the app as it changes.
EVENT_TYPES = [
    'create', 'revoke', 'role_change',
    'member.add', 'member.remove', 'member.tier_change',
    'org.create', 'org.suspend', 'org.restore', 'org.ceiling_change',
    'org.rename',
    'tier.modules_change',
    'catalog.publish', 'catalog.unpublish', 'catalog.grant', 'catalog.revoke',
    'staff.join',
]

PREVIOUS = [v for v in EVENT_TYPES if v != 'org.rename']


def _check_sql(values) -> str:
    return "event_type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type', 'access_grant_events',
                               _check_sql(EVENT_TYPES))


def downgrade() -> None:
    # Collapse rather than delete. An audit row is evidence; dropping the
    # entries that no longer fit the vocabulary would make a downgrade a way to
    # erase history, so they are relabelled to the nearest surviving verb.
    op.execute(
        "UPDATE access_grant_events SET event_type = 'org.ceiling_change' "
        "WHERE event_type = 'org.rename'"
    )
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type', 'access_grant_events',
                               _check_sql(PREVIOUS))
