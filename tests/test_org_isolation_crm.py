"""
CRM cluster: companies, contact lists, deals, events, career timeline.

Same two-sided proof as the contacts tranche — isolated from other orgs, AND
visible to colleagues in a shared org. One test per collection route, plus the
child collections that are reached through a parent.

Child tables (contact events, career timeline, company memberships) get their
own cases because they were historically scoped only by their parent's id. The
parent fetch is scoped, so that was safe — but safe by convention. These pin
the behaviour so a refactor of the parent lookup cannot quietly widen them.
"""
from datetime import date

import pytest
from sqlalchemy.orm import Session

from src.db.models import (
    CareerTimeline,
    Company,
    CompanyContact,
    Contact,
    ContactEvent,
    ContactList,
    Deal,
)
from tests.org_isolation import assert_org_isolated, client_for, make_tenant


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="acme-crm", account_id=5201, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="beta-crm", account_id=5202, data_scope="shared")


def _contact(t, email="c@crm.test"):
    return Contact(org_id=t.org_id, account_id=t.account_id,
                   first_name="C", last_name="X", email=email)


# ---------------------------------------------------------------------------
# top-level collections
# ---------------------------------------------------------------------------

async def test_companies_are_isolated(db: Session, _override_db, acme, beta):
    rows = [Company(org_id=acme.org_id, account_id=acme.account_id, name=f"Acme {i}")
            for i in range(3)]
    db.add_all(rows); db.flush()

    def seed_beta():
        theirs = Company(org_id=beta.org_id, account_id=beta.account_id, name="Beta Inc")
        db.add(theirs); db.flush()
        return [theirs.id]

    await assert_org_isolated("/api/companies/", owner=acme, intruder=beta,
                              owner_row_ids=[r.id for r in rows],
                              seed_intruder=seed_beta)


async def test_contact_lists_are_isolated(db: Session, _override_db, acme, beta):
    rows = [ContactList(org_id=acme.org_id, account_id=acme.account_id, name=f"L{i}")
            for i in range(2)]
    db.add_all(rows); db.flush()

    await assert_org_isolated("/api/contact-lists/", owner=acme, intruder=beta,
                              owner_row_ids=[r.id for r in rows])


async def test_deals_are_isolated(db: Session, _override_db, acme, beta):
    rows = [Deal(org_id=acme.org_id, account_id=acme.account_id, title=f"D{i}")
            for i in range(2)]
    db.add_all(rows); db.flush()

    await assert_org_isolated("/api/deals/", owner=acme, intruder=beta,
                              owner_row_ids=[r.id for r in rows])


# ---------------------------------------------------------------------------
# child collections, reached through a parent
# ---------------------------------------------------------------------------

async def test_contact_events_are_isolated(db: Session, _override_db, acme, beta):
    c = _contact(acme, "events@crm.test"); db.add(c); db.flush()
    ev = ContactEvent(org_id=acme.org_id, account_id=acme.account_id,
                      contact_id=c.id, event_type="note", title="private")
    db.add(ev); db.flush()

    async with client_for(acme) as cl:
        assert (await cl.get(f"/api/contacts/{c.id}/events/")).status_code == 200

    # Beta cannot reach the parent, so it cannot reach the child.
    async with client_for(beta) as cl:
        resp = await cl.get(f"/api/contacts/{c.id}/events/")
    assert resp.status_code in (403, 404), (
        f"another org reached a contact's events: {resp.status_code}")


async def test_career_timeline_is_isolated(db: Session, _override_db, acme, beta):
    c = _contact(acme, "career@crm.test"); db.add(c); db.flush()
    entry = CareerTimeline(org_id=acme.org_id, account_id=acme.account_id,
                           contact_id=c.id, title="Engineer",
                           start_date=date(2020, 1, 1))
    db.add(entry); db.flush()

    async with client_for(beta) as cl:
        resp = await cl.get(f"/api/contacts/{c.id}/career-timeline/")
    assert resp.status_code in (403, 404)


async def test_company_memberships_are_isolated(db: Session, _override_db, acme, beta):
    co = Company(org_id=acme.org_id, account_id=acme.account_id, name="Acme Co")
    c = _contact(acme, "member@crm.test")
    db.add_all([co, c]); db.flush()
    db.add(CompanyContact(org_id=acme.org_id, account_id=acme.account_id,
                          company_id=co.id, contact_id=c.id))
    db.flush()

    async with client_for(beta) as cl:
        resp = await cl.get(f"/api/companies/{co.id}/contacts/")
    assert resp.status_code in (403, 404)


# ---------------------------------------------------------------------------
# the other half: sharing actually works
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,key,factory", [
    ("/api/companies/", "companies",
     lambda t: Company(org_id=t.org_id, account_id=t.account_id, name="Shared Co")),
    ("/api/contact-lists/", "contact_lists",
     lambda t: ContactList(org_id=t.org_id, account_id=t.account_id, name="Shared List")),
    ("/api/deals/", "deals",
     lambda t: Deal(org_id=t.org_id, account_id=t.account_id, title="Shared Deal")),
])
async def test_colleague_sees_rows_they_did_not_author(
    db: Session, _override_db, acme, path, key, factory
):
    """Without this the isolation tests above could pass on a filter that
    returns nothing at all."""
    colleague = make_tenant(db, slug="acme-crm", account_id=5203, data_scope="shared")
    assert colleague.org_id == acme.org_id

    row = factory(acme); db.add(row); db.flush()

    async with client_for(colleague) as cl:
        resp = await cl.get(path)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    rows = body[key] if isinstance(body, dict) and key in body else body
    if isinstance(body, dict) and key not in body:
        rows = next(v for v in body.values() if isinstance(v, list))
    assert row.id in {r["id"] for r in rows}, f"{path}: colleague cannot see a shared row"
