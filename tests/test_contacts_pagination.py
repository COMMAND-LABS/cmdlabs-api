"""Server-side pagination contract for GET /api/contacts/.

Asserts the envelope shape ({contacts,total,limit,offset,has_more}), the
limit/offset slice, server-side search, and account isolation.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from tests.org_isolation import make_tenant
from src.db.models import Account, Contact

# Every row needs a tenant now that org_id is NOT NULL. These suites are
# single-tenant, so they all sit in the root org conftest creates.
ROOT_ORG_ID = 1

CONTACTS_URL = "/api/contacts/"


@pytest.fixture()
def seed_contacts(db: Session, test_account: Account):
    for i in range(57):
        db.add(
            Contact(
                org_id=ROOT_ORG_ID,
                account_id=test_account.id,
                first_name=f"Person{i:03d}",
                last_name="Test",
                email=f"person{i:03d}@example.com",
            )
        )
    # A contact belonging to a DIFFERENT ORG must never appear.
    #
    # This used to be a different account in the SAME org, which was invisible
    # only because the root org ran on personal scope. Since every account owns
    # its own org, a different account in your org is a colleague and their
    # contacts are meant to be visible — so that setup no longer tests
    # anything. The tenant boundary is the org, so the foreign row goes in one.
    other = make_tenant(db, slug="pagination-outsider", account_id=2)
    db.add(
        Contact(
            org_id=other.org_id,
            account_id=other.account_id,
            first_name="Foreign",
            last_name="Person",
            email="foreign@example.com",
        )
    )
    db.flush()


async def test_envelope_shape_and_default_page(
    authed_client: AsyncClient, seed_contacts
):
    resp = await authed_client.get(CONTACTS_URL)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) >= {"contacts", "total", "limit", "offset", "has_more"}
    assert body["total"] == 57          # excludes the other account's contact
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["contacts"]) == 50
    assert body["has_more"] is True


async def test_offset_limit_slice_and_last_page(
    authed_client: AsyncClient, seed_contacts
):
    resp = await authed_client.get(CONTACTS_URL, params={"limit": 25, "offset": 50})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 57
    assert body["limit"] == 25
    assert body["offset"] == 50
    assert len(body["contacts"]) == 7   # 57 - 50
    assert body["has_more"] is False


async def test_search_filters_server_side(
    authed_client: AsyncClient, seed_contacts
):
    resp = await authed_client.get(CONTACTS_URL, params={"search": "person001"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["contacts"][0]["email"] == "person001@example.com"


async def test_limit_is_capped(authed_client: AsyncClient, seed_contacts):
    # Backend Query(le=500): an over-large limit is rejected as 422.
    resp = await authed_client.get(CONTACTS_URL, params={"limit": 99999})
    assert resp.status_code == 422


async def test_sort_by_name_both_directions(
    authed_client: AsyncClient, seed_contacts
):
    resp = await authed_client.get(
        CONTACTS_URL, params={"sort_by": "name", "sort_dir": "asc", "limit": 3}
    )
    assert resp.status_code == 200
    names = [c["first_name"] for c in resp.json()["contacts"]]
    assert names == ["Person000", "Person001", "Person002"]

    resp = await authed_client.get(
        CONTACTS_URL, params={"sort_by": "name", "sort_dir": "desc", "limit": 3}
    )
    names = [c["first_name"] for c in resp.json()["contacts"]]
    assert names == ["Person056", "Person055", "Person054"]


async def test_sort_by_added_both_directions(
    authed_client: AsyncClient, seed_contacts, db: Session, test_account: Account
):
    # The seed rows share one transaction timestamp, so created_at ties across
    # the board; give them distinct instants so direction is observable.
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = (
        db.query(Contact)
        .filter(Contact.account_id == test_account.id)
        .order_by(Contact.id)
        .all()
    )
    for i, row in enumerate(rows):
        row.created_at = base + timedelta(minutes=i)
    db.flush()

    resp = await authed_client.get(
        CONTACTS_URL, params={"sort_by": "added", "sort_dir": "asc", "limit": 2}
    )
    names = [c["first_name"] for c in resp.json()["contacts"]]
    assert names == ["Person000", "Person001"]

    resp = await authed_client.get(
        CONTACTS_URL, params={"sort_by": "added", "sort_dir": "desc", "limit": 2}
    )
    names = [c["first_name"] for c in resp.json()["contacts"]]
    assert names == ["Person056", "Person055"]


async def test_sort_ties_page_deterministically(
    authed_client: AsyncClient, seed_contacts
):
    """Equal sort keys (every seed row shares last_name AND created_at) must
    not shuffle rows between pages: two adjacent pages cover the set exactly
    once. This is the Contact.id tiebreak at work."""
    first = await authed_client.get(
        CONTACTS_URL,
        params={"sort_by": "added", "sort_dir": "desc", "limit": 30, "offset": 0},
    )
    second = await authed_client.get(
        CONTACTS_URL,
        params={"sort_by": "added", "sort_dir": "desc", "limit": 30, "offset": 30},
    )
    emails = [c["email"] for c in first.json()["contacts"]] + [
        c["email"] for c in second.json()["contacts"]
    ]
    assert len(emails) == 57
    assert len(set(emails)) == 57


async def test_sort_rejects_unknown_keys(
    authed_client: AsyncClient, seed_contacts
):
    resp = await authed_client.get(CONTACTS_URL, params={"sort_by": "email"})
    assert resp.status_code == 422
    resp = await authed_client.get(
        CONTACTS_URL, params={"sort_by": "name", "sort_dir": "sideways"}
    )
    assert resp.status_code == 422
