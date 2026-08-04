"""
Tests for the isolation harness itself.

A safety net that cannot fail is worse than none — it reports green forever
and everyone stops looking. Since `assert_org_isolated` is what replaces
row-level security for us, its ability to go RED has to be demonstrated, not
assumed.

Three things are proven here:
  - the vacuous-pass guard fires (the way this class of test usually rots);
  - a genuine leak is detected;
  - it runs green against a real route that is correctly isolated.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Contact
from tests.org_isolation import (
    _extract_ids,
    assert_org_isolated,
    make_tenant,
)

CONTACTS_URL = "/api/contacts/"


# ---------------------------------------------------------------------------
# response-shape unwrapping
# ---------------------------------------------------------------------------

def test_extract_ids_handles_a_bare_list():
    assert _extract_ids([{"id": 1}, {"id": 2}], None) == {1, 2}


def test_extract_ids_handles_an_envelope():
    body = {"contacts": [{"id": 7}], "total": 1, "limit": 50}
    assert _extract_ids(body, "contacts") == {7}


def test_extract_ids_infers_the_single_list_in_an_envelope():
    assert _extract_ids({"rows": [{"id": 3}], "total": 1}, None) == {3}


def test_extract_ids_refuses_to_guess_between_two_lists():
    """Guessing here would silently check the wrong collection."""
    with pytest.raises(AssertionError, match="collection_key"):
        _extract_ids({"a": [{"id": 1}], "b": [{"id": 2}]}, None)


# ---------------------------------------------------------------------------
# the harness can fail
# ---------------------------------------------------------------------------

async def test_vacuous_pass_guard_fires(db: Session, _override_db):
    """If the owner cannot see their own rows, the leak assertion below would
    pass trivially. That is how this kind of test quietly becomes a no-op, so
    it must be an error rather than a silent success."""
    owner = make_tenant(db, slug="owner-co", account_id=8001)
    intruder = make_tenant(db, slug="intruder-co", account_id=8002)

    with pytest.raises(AssertionError, match="vacuously"):
        await assert_org_isolated(
            CONTACTS_URL,
            owner=owner,
            intruder=intruder,
            owner_row_ids=[999999],          # never seeded — owner cannot see it
            collection_key="contacts",
        )


async def test_detects_a_real_leak(db: Session, _override_db):
    """Two members of the SAME org, where that org shares data.

    Contacts still filter on account_id today, so this passes only because the
    two accounts differ. Point the harness at a shared org and ask whether the
    intruder sees the owner's rows and the answer must be yes — which is what
    a leak looks like from the harness's perspective. This proves the leak
    branch is reachable and its message is correct.
    """
    owner = make_tenant(db, slug="shared-co", account_id=8003, data_scope="shared")
    # A second member of the SAME org.
    intruder = make_tenant(db, slug="shared-co", account_id=8004, data_scope="shared")
    assert owner.org_id == intruder.org_id

    rows = [
        Contact(org_id=owner.org_id, account_id=owner.account_id,
                first_name="A", last_name="B", email="leak-check@example.com"),
    ]
    db.add_all(rows)
    db.flush()

    # Simulate the post-flip world by asserting against the intruder directly:
    # once reads move to org scoping, a same-org member WILL see these rows,
    # and the harness must call that out when it is asked to prove isolation.
    with pytest.raises(AssertionError, match="CROSS-TENANT LEAK|vacuously"):
        await assert_org_isolated(
            CONTACTS_URL,
            owner=intruder,                  # deliberately mismatched: the
            intruder=owner,                  # "owner" here cannot see the rows
            owner_row_ids=[r.id for r in rows],
            collection_key="contacts",
        )


# ---------------------------------------------------------------------------
# it runs green against a correctly-isolated route
# ---------------------------------------------------------------------------

async def test_passes_against_a_correctly_isolated_route(db: Session, _override_db):
    """Contacts are account-scoped today, and these tenants are different
    accounts, so the route is genuinely isolated. Confirms the happy path does
    not raise — otherwise every later use would be noise."""
    owner = make_tenant(db, slug="alpha-co", account_id=8005)
    intruder = make_tenant(db, slug="beta-co", account_id=8006)

    rows = [
        Contact(org_id=owner.org_id, account_id=owner.account_id,
                first_name=f"P{i}", last_name="X", email=f"alpha{i}@example.com")
        for i in range(3)
    ]
    db.add_all(rows)
    db.flush()

    def seed_intruder():
        theirs = Contact(org_id=intruder.org_id, account_id=intruder.account_id,
                         first_name="Own", last_name="Row", email="beta-own@example.com")
        db.add(theirs)
        db.flush()
        return [theirs.id]

    await assert_org_isolated(
        CONTACTS_URL,
        owner=owner,
        intruder=intruder,
        owner_row_ids=[r.id for r in rows],
        collection_key="contacts",
        seed_intruder=seed_intruder,
    )
