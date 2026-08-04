"""
Contacts tranche: reads are org-scoped.

This is the first tranche where a query's visibility actually changed, so it
gets both halves of the proof:

  - ISOLATION: another org never sees these rows (the leak case);
  - SHARING:   a colleague in the SAME shared org now DOES see them.

The second half matters as much as the first. A filter that returns nothing
is trivially "isolated" — without asserting that sharing works, an
over-restrictive filter would look like a pass.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Contact
from tests.org_isolation import assert_org_isolated, client_for, make_tenant

CONTACTS_URL = "/api/contacts/"


def _contact(org_id, account_id, email):
    return Contact(org_id=org_id, account_id=account_id,
                   first_name="T", last_name="X", email=email)


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="acme-contacts", account_id=5101, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="beta-contacts", account_id=5102, data_scope="shared")


async def test_contacts_are_isolated_between_orgs(db: Session, _override_db, acme, beta):
    rows = [_contact(acme.org_id, acme.account_id, f"acme{i}@iso.test") for i in range(3)]
    db.add_all(rows); db.flush()

    def seed_beta():
        theirs = _contact(beta.org_id, beta.account_id, "beta@iso.test")
        db.add(theirs); db.flush()
        return [theirs.id]

    await assert_org_isolated(
        CONTACTS_URL,
        owner=acme, intruder=beta,
        owner_row_ids=[r.id for r in rows],
        collection_key="contacts",
        seed_intruder=seed_beta,
    )


async def test_colleagues_in_a_shared_org_see_each_others_contacts(
    db: Session, _override_db, acme
):
    """The point of the whole migration: a three-person team sharing a CRM.

    Under the old account_id filter this returned nothing — which is exactly
    why isolation alone is not sufficient proof.
    """
    colleague = make_tenant(db, slug="acme-contacts", account_id=5103,
                            data_scope="shared")
    assert colleague.org_id == acme.org_id

    row = _contact(acme.org_id, acme.account_id, "authored-by-owner@iso.test")
    db.add(row); db.flush()

    async with client_for(colleague) as c:
        resp = await c.get(CONTACTS_URL)
    assert resp.status_code == 200, resp.text
    ids = {r["id"] for r in resp.json()["contacts"]}
    assert row.id in ids, "a shared org must show a colleague's contacts"


async def test_personal_org_members_still_do_not_see_each_other(
    db: Session, _override_db
):
    """Root is data_scope='personal'. Thousands of unrelated signups live
    there, and the flip must not have quietly introduced sharing between
    them — that would be a mass privacy break, not a feature."""
    mine = make_tenant(db, slug="rootish", account_id=5104, data_scope="personal")
    stranger = make_tenant(db, slug="rootish", account_id=5105, data_scope="personal")
    assert mine.org_id == stranger.org_id

    theirs = _contact(mine.org_id, stranger.account_id, "stranger@iso.test")
    db.add(theirs); db.flush()

    async with client_for(mine) as c:
        resp = await c.get(CONTACTS_URL)
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()["contacts"]}
    assert theirs.id not in ids, "personal scope must not leak between members"


async def test_detail_route_is_scoped_too(db: Session, _override_db, acme, beta):
    """List endpoints get the attention; a by-id GET is the easier one to
    forget, and leaks a single record just as effectively."""
    row = _contact(acme.org_id, acme.account_id, "detail@iso.test")
    db.add(row); db.flush()

    async with client_for(acme) as c:
        assert (await c.get(f"{CONTACTS_URL}{row.id}")).status_code == 200

    async with client_for(beta) as c:
        resp = await c.get(f"{CONTACTS_URL}{row.id}")
    assert resp.status_code == 404, "another org must not fetch this contact by id"


async def test_mutation_routes_are_scoped(db: Session, _override_db, acme, beta):
    """A scoped read with an unscoped write is still a breach — the intruder
    cannot see the row but can destroy or overwrite it."""
    row = _contact(acme.org_id, acme.account_id, "mutate@iso.test")
    db.add(row); db.flush()
    row_id = row.id

    async with client_for(beta) as c:
        upd = await c.put(f"{CONTACTS_URL}{row_id}", json={"first_name": "Hijacked"})
        dele = await c.delete(f"{CONTACTS_URL}{row_id}")

    assert upd.status_code == 404, "another org must not update this contact"
    assert dele.status_code == 404, "another org must not delete this contact"

    db.expire_all()
    still = db.query(Contact).filter(Contact.id == row_id).one()
    assert still.first_name == "T", "row was mutated across an org boundary"
