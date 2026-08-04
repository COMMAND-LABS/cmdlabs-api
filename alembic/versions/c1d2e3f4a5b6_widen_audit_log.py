"""Widen the audit log to cover membership, tier, org and catalog events

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-08-04 15:00:00.000000

The log recorded only resource grants — create / revoke / role_change — which
is the LEAST consequential half of what happens on the platform. Joining an
org, changing someone's tier, lowering an org's module ceiling, publishing a
lesson, and platform staff joining a tenant to read its data were all
invisible.

Reuses access_grant_events rather than adding a second table, so there is one
chronological answer to "what happened in this org, and who did it". The
table keeps its snapshot-labels-at-write-time design: actor_email,
principal_label and resource_label are copied in at write time and the id
columns carry no foreign keys, so an entry stays readable after the thing it
describes is renamed or deleted. An audit row that breaks when its subject is
deleted is precisely the row you needed.

Three changes:
  - widen event_type (the old CHECK rejected every new verb);
  - lengthen it from 20 to 40 chars for the dotted names;
  - allow a NULL principal, since org-level events (a ceiling change, a
    suspension) act on the org itself and have nobody on the other side.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'b0c1d2e3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Keep the original three: rows written before this migration still hold them,
# and rewriting history to fit a new vocabulary would defeat the point of a log.
EVENT_TYPES = (
    # resource grants (original vocabulary)
    'create', 'revoke', 'role_change',
    # organization membership
    'member.add', 'member.remove', 'member.tier_change',
    # organization lifecycle and entitlement
    'org.create', 'org.suspend', 'org.restore', 'org.ceiling_change',
    'tier.modules_change',
    # publishing
    'catalog.publish', 'catalog.unpublish', 'catalog.grant', 'catalog.revoke',
    # platform staff joining a tenant to read its data — the event that makes
    # "staff cannot read your data without appearing in your member list" a
    # checkable claim rather than a promise
    'staff.join',
)


def _check_sql(values) -> str:
    return "event_type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.alter_column('access_grant_events', 'event_type',
                    existing_type=sa.String(20), type_=sa.String(40),
                    existing_nullable=False)
    op.create_check_constraint('ck_access_grant_event_type', 'access_grant_events',
                               _check_sql(EVENT_TYPES))

    # Org-level events have no counterparty.
    op.alter_column('access_grant_events', 'principal_type',
                    existing_type=sa.String(20), nullable=True)
    op.alter_column('access_grant_events', 'principal_id',
                    existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Rows using the widened vocabulary cannot satisfy the old CHECK, and
    # deleting them to fit would be destroying an audit trail. Collapse them to
    # the nearest original verb instead, so the history survives a rollback
    # even if it loses resolution.
    op.execute("""
        UPDATE access_grant_events
           SET event_type = CASE
                 WHEN event_type IN ('member.add', 'org.create',
                                     'catalog.publish', 'catalog.grant',
                                     'staff.join') THEN 'create'
                 WHEN event_type IN ('member.remove', 'org.suspend',
                                     'catalog.unpublish', 'catalog.revoke')
                      THEN 'revoke'
                 ELSE 'role_change'
               END
         WHERE event_type NOT IN ('create', 'revoke', 'role_change')
    """)
    op.execute("""
        UPDATE access_grant_events
           SET principal_type = 'account', principal_id = 0
         WHERE principal_type IS NULL OR principal_id IS NULL
    """)
    op.alter_column('access_grant_events', 'principal_id',
                    existing_type=sa.Integer(), nullable=False)
    op.alter_column('access_grant_events', 'principal_type',
                    existing_type=sa.String(20), nullable=False)
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.alter_column('access_grant_events', 'event_type',
                    existing_type=sa.String(40), type_=sa.String(20),
                    existing_nullable=False)
    op.create_check_constraint('ck_access_grant_event_type', 'access_grant_events',
                               _check_sql(('create', 'revoke', 'role_change')))
