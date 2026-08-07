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

from src.config.modules_registry import MODULE_KEYS
from src.db.models import Organization, Contact
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


async def test_a_signup_never_shares_an_org_with_another_signup(
    db: Session, _override_db
):
    """The invariant that replaced personal scope.

    Two strangers used to land in the root org together, and a data_scope flag
    was what stopped them seeing each other's contacts — one conditional
    standing between 274 people and each other. Now they are never in the same
    org at all, so the isolation is structural rather than conditional.

    Asserted through ensure_membership rather than by constructing orgs by
    hand, because the thing that could regress is the SIGNUP PATH quietly
    putting people back in a shared org. Building the orgs here would test the
    fixture instead of the code.
    """
    from src.db.models import Account
    from src.services.organizations import ensure_membership
    from tests.org_isolation import Tenant

    a = Account(id=5104, email="stranger-a@iso.test")
    b = Account(id=5105, email="stranger-b@iso.test")
    db.add_all([a, b]); db.flush()

    ma = ensure_membership(db, a)
    mb = ensure_membership(db, b)
    assert ma.org_id != mb.org_id, "two signups must never share an org"
    assert ma.is_owner and mb.is_owner, "each owns their own workspace"

    org_b = db.query(Organization).filter(Organization.id == mb.org_id).one()
    theirs = _contact(mb.org_id, b.id, "stranger@iso.test")
    db.add(theirs); db.flush()

    # And the boundary holds over HTTP, not just in the predicate.
    org_a = db.query(Organization).filter(Organization.id == ma.org_id).one()
    # Widened the way staff actually widen a ceiling: the column AND the flag.
    # A 'subscription' ceiling is derived from the owner's plan and ignores the
    # column, so writing it alone would leave this account on the free plan and
    # the request below would 404 for the wrong reason.
    org_a.granted_modules = list(MODULE_KEYS)
    org_a.ceiling_managed_by = "grant"
    db.flush()
    async with client_for(Tenant(org=org_a, account=a)) as c:
        resp = await c.get(CONTACTS_URL)
    assert resp.status_code == 200
    assert theirs.id not in {r["id"] for r in resp.json()["contacts"]}


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
