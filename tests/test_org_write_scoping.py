"""
Writes stamp org_id.

The expand/migrate/contract ordering makes this the load-bearing step: the
column is NOT NULL only *after* every write path populates it, so a create
endpoint that forgets `org_id` shows up here rather than as a 500 in
production the day the constraint lands.

Reads still filter on account_id at this point — that flip comes later — so
these tests deliberately assert on the DATABASE ROW rather than on what a
subsequent GET returns. Asserting via the read path would pass for the wrong
reason while org_id was still NULL.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Company, Contact, Deal
from tests.org_isolation import client_for, make_tenant


@pytest.fixture()
def tenant(db: Session):
    """A member of a real (shared) org, as a client team would be."""
    return make_tenant(db, slug="write-co", account_id=7101, data_scope="shared")


async def test_created_contact_is_stamped_with_the_active_org(
    db: Session, _override_db, tenant
):
    async with client_for(tenant) as c:
        resp = await c.post("/api/contacts/", json={
            "first_name": "Ada", "last_name": "Lovelace",
            "email": "ada@write-co.test",
        })
    assert resp.status_code == 201, resp.text

    row = db.query(Contact).filter(Contact.email == "ada@write-co.test").one()
    assert row.org_id == tenant.org_id
    # account_id survives as attribution — it is no longer the tenant key.
    assert row.account_id == tenant.account_id


async def test_created_company_is_stamped_with_the_active_org(
    db: Session, _override_db, tenant
):
    async with client_for(tenant) as c:
        resp = await c.post("/api/companies/", json={"name": "Write Co"})
    assert resp.status_code == 201, resp.text

    row = db.query(Company).filter(Company.name == "Write Co").one()
    assert row.org_id == tenant.org_id


async def test_created_deal_is_stamped_with_the_active_org(
    db: Session, _override_db, tenant
):
    async with client_for(tenant) as c:
        resp = await c.post("/api/deals/", json={"title": "Big Deal"})
    assert resp.status_code == 201, resp.text

    row = db.query(Deal).filter(Deal.title == "Big Deal").one()
    assert row.org_id == tenant.org_id


async def test_two_orgs_stamp_their_own_id(db: Session, _override_db, tenant):
    """The stamp follows the ACTIVE org, not the account.

    Without this, a single-tenant test would pass against a hardcoded org id
    and the bug would only appear once a second org existed.
    """
    other = make_tenant(db, slug="other-co", account_id=7102, data_scope="shared")
    assert other.org_id != tenant.org_id

    for t, email in ((tenant, "a@t1.test"), (other, "b@t2.test")):
        async with client_for(t) as c:
            resp = await c.post("/api/contacts/", json={
                "first_name": "X", "last_name": "Y", "email": email,
            })
        assert resp.status_code == 201, resp.text

    assert db.query(Contact).filter(Contact.email == "a@t1.test").one().org_id == tenant.org_id
    assert db.query(Contact).filter(Contact.email == "b@t2.test").one().org_id == other.org_id


async def test_account_without_a_membership_cannot_write(db: Session, _override_db):
    """Fails closed. An account with no org has nowhere to put the row, and
    inventing one would silently misfile it."""
    from src.db.models import Account

    orphan = Account(id=7199, email="orphan@write.test")
    db.add(orphan)
    db.flush()

    from tests.conftest import make_token
    from httpx import ASGITransport, AsyncClient
    from src.main import app

    token = make_token(email=orphan.email, user_id=orphan.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        resp = await c.post("/api/contacts/", json={
            "first_name": "No", "last_name": "Org", "email": "noorg@write.test",
        })
    assert resp.status_code == 403, resp.text
    assert db.query(Contact).filter(Contact.email == "noorg@write.test").count() == 0
