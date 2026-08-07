"""staff becomes super admin

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d5

ONE ROLE HAD THREE NAMES.

    accounts.is_staff          the stored fact
    is_super_admin / require_super_admin   the code that read it
    'staff.join'               the audit event it produced

All three meant "platform operator", and nothing in the system distinguished
them — the same boolean answered all three. Three names for one concept is the
same defect as three ways to reach a course: whichever one you learn first, you
have to learn the other two before you can read the code. `super_admin` wins
because two of the three already used it, and because it is the word the people
who hold it use.

WHAT MOVES
----------
    accounts.is_staff       -> accounts.is_super_admin   (column + index)
    'staff.join'            -> 'super_admin.join'        (CHECK + existing rows)

THE AUDIT ROWS ARE REWRITTEN, WHICH DESERVES A NOTE. An audit log records what
happened, and rewriting it is normally the one thing it must not permit. What
is being changed here is the NAME OF THE EVENT TYPE, not any claim about who
did what, when, or to which org — actor_account_id, org_id and created_at are
untouched, and no row is added or removed. Every existing entry still says
exactly what it said: this operator joined this tenant at this instant. Leaving
the old value behind would mean the CHECK had to permit both spellings forever,
and "who joined our org?" would need a query that knew about a rename.

The SNAPSHOTTED LABELS on these rows are a different thing and stay untouched:
those record what something was CALLED at the time, which is the point of
snapshotting them.

Create Date: 2026-08-07
"""
from alembic import op
import sqlalchemy as sa


revision = 'f1a2b3c4d5e6'
down_revision = 'e9f0a1b2c3d5'
branch_labels = None
depends_on = None


# Kept verbatim in step with db/models.py AccessGrantEvent and services/audit.py.
# Spelled out rather than built from a list so that reading this migration tells
# you what the constraint was, without resolving anything.
EVENT_TYPES_NEW = (
    "'create','revoke','role_change',"
    "'member.add','member.remove','member.tier_change',"
    "'org.create','org.suspend','org.restore','org.ceiling_change',"
    "'org.rename',"
    "'tier.modules_change',"
    "'catalog.publish','catalog.unpublish','catalog.grant','catalog.revoke',"
    "'super_admin.join',"
    "'space.create','space.archive',"
    "'space.member_add','space.member_remove',"
    "'space.request','space.request_approve','space.request_deny',"
    "'space.resource_add','space.resource_remove'"
)
EVENT_TYPES_OLD = EVENT_TYPES_NEW.replace("'super_admin.join'", "'staff.join'")


def _swap_event_type(old: str, new: str, allowed: str) -> None:
    """Drop the CHECK, move the rows, put the CHECK back.

    In that order on purpose: the UPDATE writes a value the old constraint
    forbids, so a migration that renamed the rows first would fail against its
    own database.
    """
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    moved = op.get_bind().execute(
        sa.text("UPDATE access_grant_events SET event_type = :new "
                "WHERE event_type = :old"),
        {"new": new, "old": old}).rowcount
    op.create_check_constraint(
        'ck_access_grant_event_type', 'access_grant_events',
        f"event_type IN ({allowed})")
    print(f"[audit] {moved} '{old}' event(s) renamed to '{new}'")


def upgrade():
    op.alter_column('accounts', 'is_staff', new_column_name='is_super_admin')
    op.execute('ALTER INDEX ix_accounts_is_staff '
               'RENAME TO ix_accounts_is_super_admin')
    _swap_event_type('staff.join', 'super_admin.join', EVENT_TYPES_NEW)


def downgrade():
    _swap_event_type('super_admin.join', 'staff.join', EVENT_TYPES_OLD)
    op.execute('ALTER INDEX ix_accounts_is_super_admin '
               'RENAME TO ix_accounts_is_staff')
    op.alter_column('accounts', 'is_super_admin', new_column_name='is_staff')
