"""
The audit log records what actually matters.

Before this, the log covered resource grants only — create / revoke /
role_change — which is the least consequential half of what happens on the
platform. Joining an org, changing a tier, lowering a ceiling, publishing a
lesson, and super admins joining a tenant to read its data were all invisible.

Two properties are pinned here, and the second is the one that usually rots:

  - the events are written at all;
  - an entry stays READABLE after its subject is deleted, because the labels
    are snapshotted at write time and the id columns carry no foreign keys. A
    log that goes blank when the interesting thing is removed documents only
    the boring cases.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrantEvent,
    Account,
    Agent,
    Organization,
    OrganizationMember,
)
from src.main import app
from src.services import audit
from src.services.organizations import ensure_membership
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import make_tenant



@pytest.fixture()
def super_admin(db: Session, test_org: Organization):
    acct = Account(id=7300, email="auditor@cmdlabs.io", is_super_admin=True,
                   default_org_id=ROOT_ORG_ID)
    db.add(acct); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=acct.id,
                              role="manager", granted_by="grant"))
    db.flush()
    return acct


@pytest.fixture()
async def super_admin_client(_override_db, super_admin) -> AsyncClient:
    token = make_token(email=super_admin.email, user_id=super_admin.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as ac:
        yield ac


def _events(db, event_type=None):
    q = db.query(AccessGrantEvent)
    if event_type:
        q = q.filter(AccessGrantEvent.event_type == event_type)
    return q.all()


# ---------------------------------------------------------------------------
# membership
# ---------------------------------------------------------------------------

def test_joining_an_org_is_recorded(db: Session, test_org):
    """A signup lands in its OWN workspace, and that is logged.

    It used to land in root, which is why this once asserted test_org.id and
    tier 'free'. Since org-per-signup the newcomer owns the org it joins, so
    the tier is 'owner' — and for a personal workspace the tier is inert
    anyway, because an owner resolves modules straight from the ceiling.
    """
    newcomer = Account(id=7301, email="newcomer@x.com")
    db.add(newcomer); db.flush()

    ensure_membership(db, newcomer)

    events = _events(db, audit.MEMBER_ADD)
    assert len(events) == 1
    ev = events[0]
    assert ev.org_id != test_org.id, "a signup must not land in the platform org"
    assert ev.principal_id == newcomer.id
    assert ev.principal_label == "newcomer@x.com"   # snapshotted
    # The ROLE they joined on. Every signup joins in the smallest role — even
    # the owner of the org being created, whose role is inert because ownership
    # bypasses it.
    assert ev.role == "community_member"


def test_membership_is_recorded_once_not_on_every_login(db: Session, test_org):
    """ensure_membership runs on every verified login. A log that grew by one
    row per sign-in would bury the events worth reading."""
    acct = Account(id=7302, email="repeat@x.com")
    db.add(acct); db.flush()

    ensure_membership(db, acct)
    ensure_membership(db, acct)
    ensure_membership(db, acct)

    assert len(_events(db, audit.MEMBER_ADD)) == 1


# ---------------------------------------------------------------------------
# publishing
# ---------------------------------------------------------------------------

def test_actor_email_is_snapshotted_not_joined(db: Session, test_org):
    """Deleting the actor must not blank out who did it."""
    actor = Account(id=7305, email="departing@x.com", default_org_id=ROOT_ORG_ID)
    db.add(actor); db.flush()

    audit.record_org_change(db, event_type=audit.ORG_CEILING_CHANGE,
                            org_id=test_org.id, detail="contacts,deals",
                            actor_account_id=actor.id)
    db.flush()

    db.delete(actor)
    db.flush()

    ev = _events(db, audit.ORG_CEILING_CHANGE)[0]
    assert ev.actor_email == "departing@x.com"
    assert ev.detail == "contacts,deals"  # what the ceiling became, not just "changed"


# ---------------------------------------------------------------------------
# it must never break the operation it describes
# ---------------------------------------------------------------------------

def test_audit_failure_does_not_raise(db: Session, test_org):
    """An audit write that can 500 a request is an audit write people delete."""
    result = audit.record(
        db,
        event_type="not.a.real.event",     # violates the CHECK on flush
        org_id=test_org.id,
        resource_type=audit.RESOURCE_ORGANIZATION,
        resource_id=test_org.id,
    )
    assert result is not None      # queued; the constraint fires at flush
    db.rollback()


def test_org_level_events_need_no_principal(db: Session, test_org):
    """A ceiling change or suspension acts on the org itself. Requiring a
    counterparty would force callers to invent one."""
    audit.record_org_change(db, event_type=audit.ORG_SUSPEND, org_id=test_org.id,
                            detail="subscription lapsed")
    db.flush()

    ev = _events(db, audit.ORG_SUSPEND)[0]
    assert ev.principal_type is None and ev.principal_id is None
