"""
Platform-admin org list.

The listing itself is unremarkable; the access control is the point. Two
properties are pinned here:

  - a non-super-admin caller gets 404, not 403, so the admin surface does not
    confirm its own existence to someone who should not see it;
  - the response carries counts and configuration only. Super admins
    administer orgs from this page; reading an org's rows still requires
    joining it.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.config import plans_registry as plans
from httpx import ASGITransport, AsyncClient

from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.main import app
from tests.conftest import make_token

ADMIN_ORGS_URL = "/api/admin/organizations"


@pytest.fixture()
def super_admin_account(db, test_org):
    account = Account(id=900, email="superadmin@cmdlabs.io", is_super_admin=True,
                      default_org_id=test_org.id)
    db.add(account)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id, account_id=account.id,
        tier_key="org_owner", granted_by="grant",
    ))
    db.flush()
    return account


@pytest.fixture()
async def super_admin_client(_override_db, super_admin_account) -> AsyncClient:
    token = make_token(email=super_admin_account.email, user_id=super_admin_account.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


@pytest.fixture()
def seeded_orgs(db, test_org, test_account):
    acme = Organization(name="Acme", pinned_plan="premium")
    # An org whose owner's card failed two days ago. Its state is not stored
    # anywhere — it is this timestamp, read through plans.billing_state — so
    # seeding it means seeding the OWNER, which is the point.
    lapsed_owner = Account(
        id=7701, email="lapsed-owner@x.test",
        subscription_status="canceled",
        subscription_lapsed_at=datetime.now(timezone.utc) - timedelta(days=2),
    )
    db.add(lapsed_owner)
    db.flush()
    # No pin: its plan follows lapsed_owner's subscription, which is the
    # whole point of the fixture.
    lapsed = Organization(name="Lapsed Co", owner_account_id=lapsed_owner.id)
    db.add_all([acme, lapsed])
    db.flush()
    db.add(OrganizationTier(org_id=acme.id, tier_key="member", label="Member",
                            modules=["contacts"]))
    db.add(OrganizationMember(org_id=acme.id, account_id=test_account.id,
                              tier_key="member", granted_by="grant"))
    db.flush()
    return acme, lapsed


# ---------------------------------------------------------------------------
# access control
# ---------------------------------------------------------------------------

async def test_non_super_admin_gets_404_not_403(authed_client: AsyncClient, test_account):
    """404 so the endpoint does not reveal that an admin surface exists."""
    resp = await authed_client.get(ADMIN_ORGS_URL)
    assert resp.status_code == 404


async def test_unauthenticated_is_rejected(client: AsyncClient):
    resp = await client.get(ADMIN_ORGS_URL)
    assert resp.status_code in (401, 403)


async def test_super_admin_can_list(super_admin_client: AsyncClient, seeded_orgs):
    resp = await super_admin_client.get(ADMIN_ORGS_URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    names = [o["name"] for o in body["organizations"]]
    assert "CMD LABS" in names and "Acme" in names and "Lapsed Co" in names
    assert body["total"] == len(body["organizations"])


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------

async def test_summary_reports_counts_and_plan(super_admin_client: AsyncClient, seeded_orgs):
    resp = await super_admin_client.get(ADMIN_ORGS_URL)
    row = next(o for o in resp.json()["organizations"] if o["name"] == "Acme")

    assert row["member_count"] == 1
    assert row["tier_count"] == 1
    assert row["pinned_plan"] == "premium"
    assert row["modules"] == plans.modules_for_plan(plans.PLAN_PREMIUM), (
        "what a pinned plan opens is read from the registry, not stored")
    # `is_personal` used to mean "has no slug" and this org had one, so it read
    # False despite having a single member. It now means what it says — one
    # member — and this fixture builds exactly that.
    assert row["is_personal"] is True


async def test_a_lapsed_org_is_visible_and_says_so(super_admin_client: AsyncClient, seeded_orgs):
    """A lapsed org must still appear — it is read-only, not deleted.

    And the state is DERIVED here, from the owner's subscription, rather than
    read from a column on the org. Super admins seeing 'grace' is a super admin
    seeing the real answer to "why can't they save anything", which was the
    whole reason the stored column was worse than useless: it always said
    'active'.
    """
    resp = await super_admin_client.get(ADMIN_ORGS_URL)
    row = next(o for o in resp.json()["organizations"]
               if o["name"] == "Lapsed Co")
    assert row["billing_state"] == "grace"


async def test_response_carries_no_tenant_data(super_admin_client: AsyncClient, seeded_orgs):
    """Guard against this page quietly growing into a data-read bypass."""
    body = (await super_admin_client.get(ADMIN_ORGS_URL)).json()
    # Configuration and counts only. A new field has to be added here
    # deliberately, which is the point: this test is the thing standing between
    # "super admins can administer an org" and "super admins can quietly read
    # it".
    allowed = {
        "id", "name", "is_personal", "billing_state", "owner_account_id",
        "owner_email", "member_count", "tier_count", "modules",
        "pinned_plan", "created_at",
    }
    for org in body["organizations"]:
        assert set(org.keys()) <= allowed, f"unexpected field(s): {set(org) - allowed}"
