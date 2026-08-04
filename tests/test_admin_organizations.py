"""
Platform-admin org list.

The listing itself is unremarkable; the access control is the point. Two
properties are pinned here:

  - a non-staff caller gets 404, not 403, so the admin surface does not
    confirm its own existence to someone who should not see it;
  - the response carries counts and configuration only. Staff administer orgs
    from this page; reading an org's rows still requires joining it.
"""
import pytest
from httpx import ASGITransport, AsyncClient

from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.main import app
from tests.conftest import make_token

ADMIN_ORGS_URL = "/api/admin/organizations"


@pytest.fixture()
def staff_account(db, test_org):
    account = Account(id=900, email="staff@cmdlabs.io", role="admin",
                      default_org_id=test_org.id)
    db.add(account)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id, account_id=account.id,
        tier_key="org_owner", granted_by="grant", is_owner=True,
    ))
    db.flush()
    return account


@pytest.fixture()
async def staff_client(_override_db, staff_account) -> AsyncClient:
    token = make_token(email=staff_account.email, user_id=staff_account.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture()
def seeded_orgs(db, test_org, test_account):
    acme = Organization(slug="acme", name="Acme", data_scope="shared",
                        granted_modules=["contacts", "deals"], status="active")
    lapsed = Organization(slug="lapsed-co", name="Lapsed Co", data_scope="shared",
                          granted_modules=["contacts"], status="read_only")
    db.add_all([acme, lapsed])
    db.flush()
    db.add(OrganizationTier(org_id=acme.id, tier_key="member", label="Member",
                            modules=["contacts"]))
    db.add(OrganizationMember(org_id=acme.id, account_id=test_account.id,
                              tier_key="member", granted_by="grant", is_owner=False))
    db.flush()
    return acme, lapsed


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------

async def test_non_staff_gets_404_not_403(authed_client: AsyncClient, test_account):
    """404 so the endpoint does not reveal that an admin surface exists."""
    resp = await authed_client.get(ADMIN_ORGS_URL)
    assert resp.status_code == 404


async def test_unauthenticated_is_rejected(client: AsyncClient):
    resp = await client.get(ADMIN_ORGS_URL)
    assert resp.status_code in (401, 403)


async def test_staff_can_list(staff_client: AsyncClient, seeded_orgs):
    resp = await staff_client.get(ADMIN_ORGS_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    slugs = [o["slug"] for o in body["organizations"]]
    assert "root" in slugs and "acme" in slugs and "lapsed-co" in slugs
    assert body["total"] == len(body["organizations"])


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------

async def test_summary_reports_counts_and_ceiling(staff_client: AsyncClient, seeded_orgs):
    resp = await staff_client.get(ADMIN_ORGS_URL)
    row = next(o for o in resp.json()["organizations"] if o["slug"] == "acme")

    assert row["member_count"] == 1
    assert row["tier_count"] == 1
    assert row["granted_modules"] == ["contacts", "deals"]
    assert row["data_scope"] == "shared"


async def test_suspended_org_is_visible_as_read_only(staff_client: AsyncClient, seeded_orgs):
    """A lapsed org must still appear — it is suspended, not deleted."""
    resp = await staff_client.get(ADMIN_ORGS_URL)
    row = next(o for o in resp.json()["organizations"] if o["slug"] == "lapsed-co")
    assert row["status"] == "read_only"


async def test_response_carries_no_tenant_data(staff_client: AsyncClient, seeded_orgs):
    """Guard against this page quietly growing into a data-read bypass."""
    body = (await staff_client.get(ADMIN_ORGS_URL)).json()
    allowed = {
        "id", "slug", "name", "data_scope", "status", "owner_account_id",
        "owner_email", "member_count", "tier_count", "granted_modules",
        "created_at",
    }
    for org in body["organizations"]:
        assert set(org.keys()) <= allowed, f"unexpected field(s): {set(org) - allowed}"
