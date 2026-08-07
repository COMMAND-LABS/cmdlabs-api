"""allow space.resource_add / space.resource_remove audit events

Revision ID: c7d8e9f0a1b3
Revises: b6c7d8e9f0a2

A BUG THAT COULD ONLY BE SILENT.

Sharing something into a space, and un-sharing it, both call
audit.record_space with an event_type the CHECK constraint on
access_grant_events does not permit. The event types were added to the MODEL
when spaces gained resources and never to the database.

Nothing failed visibly, because services/audit.record deliberately never
raises: an audit write must not fail the operation being audited. So every
share and un-share since has logged an ERROR and dropped the event — the one
class of bug this table exists to prevent, in the table itself.

Found by making the test database run these migrations instead of
Base.metadata.create_all. The old bootstrap reconciled CHECK constraints from
the models by hand, which papered over exactly this: the tests saw the model's
constraint and the database kept its own.
"""
from alembic import op


revision = 'c7d8e9f0a1b3'
down_revision = 'b6c7d8e9f0a2'
branch_labels = None
depends_on = None

_EVENT_TYPES = (
    "'create','revoke','role_change',"
    "'member.add','member.remove','member.tier_change',"
    "'org.create','org.suspend','org.restore','org.ceiling_change',"
    "'org.rename',"
    "'tier.modules_change',"
    "'catalog.publish','catalog.unpublish','catalog.grant','catalog.revoke',"
    "'staff.join',"
    "'space.create','space.archive',"
    "'space.member_add','space.member_remove',"
    "'space.request','space.request_approve','space.request_deny',"
    "'space.resource_add','space.resource_remove'"
)

# Everything above except the two being added.
_WITHOUT_RESOURCE_EVENTS = _EVENT_TYPES.replace(
    ",'space.resource_add','space.resource_remove'", "")


def upgrade():
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_event_type', 'access_grant_events',
        f"event_type IN ({_EVENT_TYPES})")


def downgrade():
    # Drop any rows the widened constraint allowed, or the narrower one cannot
    # be re-created. They are audit records, so this is a real loss and the
    # reason to think twice before downgrading past here.
    op.execute("DELETE FROM access_grant_events WHERE event_type IN "
               "('space.resource_add','space.resource_remove')")
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint(
        'ck_access_grant_event_type', 'access_grant_events',
        f"event_type IN ({_WITHOUT_RESOURCE_EVENTS})")
