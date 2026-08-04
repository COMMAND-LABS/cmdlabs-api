"""
Publishing: platform -> tenant, one direction only.

The catalog is the one place content deliberately crosses an org boundary, so
the guard on it carries the whole design. `assert_publishable` refusing a
tenant-owned resource is what makes publishing one-directional; without it an
org owner could publish their own agent, have it granted to a competitor, and
the catalog would be exactly the cross-tenant channel it exists to avoid.

End to end here: publish, grant to an org and to a department, and verify who
can actually see the lesson afterwards.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.db.catalog_models import CatalogGrant, CatalogItem
from src.db.models import (
    AccessGroup,
    AccessGroupMember,
    Account,
    Agent,
    Contact,
    Organization,
    OrganizationMember,
)
from src.main import app
from src.services import catalog
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import client_for, make_tenant

CATALOG_URL = "/api/admin/catalog"


@pytest.fixture()
def staff(db: Session, test_org: Organization):
    """Platform staff, sitting in the root (platform) org."""
    acct = Account(id=6900, email="staff@cmdlabs.io", role="admin",
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


@pytest.fixture()
def platform_lesson(db: Session, staff):
    """An agent owned by the PLATFORM org — publishable."""
    a = Agent(org_id=ROOT_ORG_ID, account_id=staff.id, name="Lesson 1",
              visibility="private", config={"data": {}})
    db.add(a); db.flush()
    return a


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="cat-acme", account_id=6901, data_scope="shared")


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

async def test_cannot_publish_a_tenant_owned_resource(
    db: Session, _override_db, staff_client, acme
):
    """The check the whole design rests on."""
    theirs = Agent(org_id=acme.org_id, account_id=acme.account_id,
                   name="Acme Private", visibility="private", config={"data": {}})
    db.add(theirs); db.flush()

    resp = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": theirs.id, "title": "Stolen",
    })
    assert resp.status_code == 422, resp.text
    assert "another organization" in resp.json()["detail"]
    assert db.query(CatalogItem).count() == 0


async def test_cannot_publish_a_signup_s_own_agent(
    db: Session, _override_db, staff_client
):
    """Belonging to the root org is not by itself proof of platform ownership.

    The platform org and the public-signup org are the same row: every free
    account lives in root and its agents carry org_id = root. An org check
    alone would therefore have classified a stranger's private agent as
    publishable platform content, and staff could have pushed it into a client
    org. The author's role is what actually separates the two.
    """
    signup = Account(id=6950, email="member@public.test", role="free",
                     default_org_id=ROOT_ORG_ID)
    db.add(signup); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=signup.id,
                              tier_key="free", granted_by="grant", is_owner=False))
    theirs = Agent(org_id=ROOT_ORG_ID, account_id=signup.id, name="My Notes",
                   visibility="private", config={"data": {}})
    db.add(theirs); db.flush()

    resp = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": theirs.id, "title": "Not ours",
    })
    assert resp.status_code == 422, resp.text
    assert "platform staff" in resp.json()["detail"]
    assert db.query(CatalogItem).count() == 0


async def test_cannot_publish_a_crm_resource_type(
    db: Session, _override_db, staff_client, staff
):
    """Contacts are tenant data. Even from the platform org, they must never
    become publishable by passing a different type string."""
    c = Contact(org_id=ROOT_ORG_ID, account_id=staff.id, first_name="A",
                last_name="B", email="nope@cat.test")
    db.add(c); db.flush()

    resp = await staff_client.post(CATALOG_URL, json={
        "resource_type": "contact", "resource_id": c.id, "title": "Nope",
    })
    assert resp.status_code == 422
    assert "not publishable" in resp.json()["detail"]


async def test_cannot_publish_a_missing_resource(_override_db, staff_client):
    resp = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": 999999, "title": "Ghost",
    })
    assert resp.status_code == 422
    assert "does not exist" in resp.json()["detail"]


async def test_non_staff_cannot_reach_the_catalog(_override_db, authed_client):
    """404, not 403 — the admin surface does not confirm it exists."""
    assert (await authed_client.get(CATALOG_URL)).status_code == 404
    assert (await authed_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": 1, "title": "x",
    })).status_code == 404


# ---------------------------------------------------------------------------
# publish + grant, end to end
# ---------------------------------------------------------------------------

async def test_publish_then_grant_makes_the_lesson_visible(
    db: Session, _override_db, staff_client, platform_lesson, acme
):
    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": platform_lesson.id,
        "title": "Lesson 1", "description": "Fundamentals",
    })
    assert pub.status_code == 201, pub.text
    item_id = pub.json()["id"]

    async with client_for(acme) as c:
        before = {a["id"] for a in (await c.get("/api/agents/")).json()}
    assert platform_lesson.id not in before, "publishing alone must not grant access"

    gr = await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                 json={"org_id": acme.org_id})
    assert gr.status_code == 201, gr.text

    async with client_for(acme) as c:
        after = {a["id"] for a in (await c.get("/api/agents/")).json()}
    assert platform_lesson.id in after


async def test_revoking_a_grant_removes_access(
    db: Session, _override_db, staff_client, platform_lesson, acme
):
    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": platform_lesson.id, "title": "L"})
    item_id = pub.json()["id"]
    gr = await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                 json={"org_id": acme.org_id})
    grant_id = gr.json()["id"]

    revoke = await staff_client.delete(f"{CATALOG_URL}/{item_id}/grants/{grant_id}")
    assert revoke.status_code == 204

    async with client_for(acme) as c:
        visible = {a["id"] for a in (await c.get("/api/agents/")).json()}
    assert platform_lesson.id not in visible


async def test_group_grant_must_name_a_group_in_that_org(
    db: Session, _override_db, staff_client, platform_lesson, acme
):
    """Otherwise a grant could name Acme's org and Beta's 'Sales' group, and
    membership of BETA's group would decide who in Acme sees the lesson."""
    beta = make_tenant(db, slug="cat-beta", account_id=6902, data_scope="shared")
    their_group = AccessGroup(name="Beta Sales", owner_account_id=beta.account_id,
                              org_id=beta.org_id)
    db.add(their_group); db.flush()

    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": platform_lesson.id, "title": "L"})
    item_id = pub.json()["id"]

    resp = await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                   json={"org_id": acme.org_id,
                                         "group_id": their_group.id})
    assert resp.status_code == 422
    assert "does not belong" in resp.json()["detail"]


async def test_publishing_twice_is_idempotent(
    db: Session, _override_db, staff_client, platform_lesson
):
    """Two entries for one resource would mean two independently-revocable
    grant surfaces — revoking one would look like it worked while access
    continued through the other."""
    for title in ("First", "Second"):
        r = await staff_client.post(CATALOG_URL, json={
            "resource_type": "agent", "resource_id": platform_lesson.id,
            "title": title})
        assert r.status_code == 201, r.text

    assert db.query(CatalogItem).filter(
        CatalogItem.resource_id == platform_lesson.id).count() == 1


async def test_unpublish_cascades_its_grants(
    db: Session, _override_db, staff_client, platform_lesson, acme
):
    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": platform_lesson.id, "title": "L"})
    item_id = pub.json()["id"]
    await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                            json={"org_id": acme.org_id})

    assert (await staff_client.delete(f"{CATALOG_URL}/{item_id}")).status_code == 204
    assert db.query(CatalogGrant).filter(
        CatalogGrant.catalog_item_id == item_id).count() == 0

    async with client_for(acme) as c:
        visible = {a["id"] for a in (await c.get("/api/agents/")).json()}
    assert platform_lesson.id not in visible


# ---------------------------------------------------------------------------
# the training scenario
# ---------------------------------------------------------------------------

async def test_department_scoped_lesson_reaches_only_that_department(
    db: Session, _override_db, staff_client, platform_lesson, acme
):
    sales = AccessGroup(name="Sales", owner_account_id=acme.account_id,
                        org_id=acme.org_id)
    db.add(sales); db.flush()

    in_sales = make_tenant(db, slug="cat-acme", account_id=6903, data_scope="shared")
    engineering = make_tenant(db, slug="cat-acme", account_id=6904, data_scope="shared")
    db.add(AccessGroupMember(access_group_id=sales.id,
                             account_id=in_sales.account_id, role="member"))
    db.flush()

    pub = await staff_client.post(CATALOG_URL, json={
        "resource_type": "agent", "resource_id": platform_lesson.id,
        "title": "Lesson 5 — Sales"})
    item_id = pub.json()["id"]
    gr = await staff_client.post(f"{CATALOG_URL}/{item_id}/grants",
                                 json={"org_id": acme.org_id, "group_id": sales.id})
    assert gr.status_code == 201, gr.text

    async with client_for(in_sales) as c:
        sales_sees = {a["id"] for a in (await c.get("/api/agents/")).json()}
    async with client_for(engineering) as c:
        eng_sees = {a["id"] for a in (await c.get("/api/agents/")).json()}

    assert platform_lesson.id in sales_sees
    assert platform_lesson.id not in eng_sees
