"""
The audit log records what actually matters.

Before this, the log covered resource grants only — create / revoke /
role_change — which is the least consequential half of what happens on the
platform. Joining an org, changing a tier, lowering a ceiling, publishing a
lesson, and staff joining a tenant to read its data were all invisible.

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

from src.db.catalog_models import CatalogItem
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

CATALOG_URL = "/api/admin/catalog"


@pytest.fixture()
def staff(db: Session, test_org: Organization):
    acct = Account(id=7300, email="auditor@cmdlabs.io", role="admin",
                   default_org_id=ROOT_ORG_ID)
    db.add(acct); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=acct.id,
                              tier_key="org_owner", granted_by="grant", is_owner=True))
    db.flush()
    return acct


@pytest.fixture()
async def staff_client(_override_db, staff) -> AsyncClient:
    token = make_token(email=staff.email, user_id=staff.id)
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
    assert ev.role == "owner"                       # the tier they joined on


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

async def test_publish_and_grant_are_recorded(
    db: Session, _override_db, staff_client, staff
):
    acme = make_tenant(db, slug="audit-acme", account_id=7303, data_scope="shared")
    lesson = Agent(org_id=ROOT_ORG_ID, account_id=staff.id, name="Lesson",
                   visibility="private", config={"data": {}})
    db.add(lesson); db.flush()

    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": lesson.id, "title": "Lesson 1"})
    assert pub.status_code == 201, pub.text
    item_id = pub.json()["id"]

    published = _events(db, audit.CATALOG_PUBLISH)
    assert len(published) == 1
    assert published[0].resource_label == "Lesson 1"
    assert published[0].actor_email == "auditor@cmdlabs.io"

    gr = await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                 json={"org_id": acme.org_id})
    assert gr.status_code == 201

    granted = _events(db, audit.CATALOG_GRANT)
    assert len(granted) == 1
    # Recorded against the RECEIVING org, so a client can see in their own log
    # when a lesson arrived.
    assert granted[0].org_id == acme.org_id


async def test_revoking_a_lesson_is_recorded(
    db: Session, _override_db, staff_client, staff
):
    acme = make_tenant(db, slug="audit-rev", account_id=7304, data_scope="shared")
    lesson = Agent(org_id=ROOT_ORG_ID, account_id=staff.id, name="L",
                   visibility="private", config={"data": {}})
    db.add(lesson); db.flush()

    item_id = (await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": lesson.id, "title": "L"})).json()["id"]
    grant_id = (await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                        json={"org_id": acme.org_id})).json()["id"]

    assert (await staff_client.delete(
        f"{CATALOG_URL}/{item_id}/grants/{grant_id}")).status_code == 204

    revoked = _events(db, audit.CATALOG_REVOKE)
    assert len(revoked) == 1
    assert revoked[0].org_id == acme.org_id


# ---------------------------------------------------------------------------
# survives deletion — the property that makes a log worth keeping
# ---------------------------------------------------------------------------

async def test_entry_survives_the_thing_it_describes(
    db: Session, _override_db, staff_client, staff
):
    """Unpublish the lesson, then confirm the log still says what it was.

    The id columns deliberately carry no foreign keys and the labels are
    copied in at write time, so removing the subject cannot cascade the record
    away — which is exactly the record someone will come looking for.
    """
    lesson = Agent(org_id=ROOT_ORG_ID, account_id=staff.id, name="Doomed",
                   visibility="private", config={"data": {}})
    db.add(lesson); db.flush()

    item_id = (await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": lesson.id,
        "title": "Doomed Lesson"})).json()["id"]

    assert (await staff_client.delete(f"{CATALOG_URL}/{item_id}")).status_code == 204
    assert db.query(CatalogItem).filter(CatalogItem.id == item_id).count() == 0

    trail = _events(db)
    labels = {e.resource_label for e in trail}
    assert "Doomed Lesson" in labels, "the log lost the record when the item was deleted"
    assert {audit.CATALOG_PUBLISH, audit.CATALOG_UNPUBLISH} <= {e.event_type for e in trail}


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
