"""
GET /api/organizations/mine — the caller's own memberships, across orgs.

WHY THIS FILE EXISTS SEPARATELY from the other org suites: this endpoint is the
one read that is deliberately NOT org-scoped. Everywhere else
org_scope.tenant_predicate adds `org_id == ctx.org_id` behind whatever the
route asks for, so a forgotten filter still cannot cross a tenant boundary.
Here the filter on OrganizationMember.account_id is the entire boundary, with
nothing behind it — answering "which orgs am I in" across orgs is the point.

So the tests below are less about the happy path than about the SHAPE of what
comes back: the caller's own membership rows and nothing else. The endpoint
feeds both the org switcher and the account-settings card, and the second is
what motivated tier_key — a member could previously see their tier nowhere at
all, because /me/entitlements covers only the active org and the owner's
console 404s for them.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import OrganizationTier
from tests.org_isolation import client_for, make_tenant

MINE = "/api/organizations/mine"


@pytest.fixture()
def acme(db: Session):
    """An org whose owner holds the 'owner' tier."""
    return make_tenant(db, slug="mine-acme", account_id=9601, tier_key="owner",
                       is_owner=True)


@pytest.fixture()
def colleague(db: Session, acme):
    """A plain member of the SAME org, on a different tier."""
    return make_tenant(db, slug="mine-acme", account_id=9602,
                       tier_key="analyst", is_owner=False)


async def test_lists_the_callers_own_membership_with_its_tier(
    db: Session, _override_db, acme
):
    async with client_for(acme) as c:
        resp = await c.get(MINE)

    assert resp.status_code == 200, resp.text
    orgs = resp.json()["organizations"]
    mine = next(o for o in orgs if o["id"] == acme.org_id)
    assert mine["tier_key"] == "owner"
    assert mine["is_owner"] is True


async def test_the_tier_carries_the_owners_label_not_the_raw_key(
    db: Session, _override_db, acme
):
    """A key is an identifier; a label is what a person should be shown.

    tier_label is what the settings card renders. Without it the UI would fall
    back to the key, which is how internal vocabulary leaks into the product.
    """
    tier = (db.query(OrganizationTier)
              .filter(OrganizationTier.org_id == acme.org_id,
                      OrganizationTier.tier_key == "owner").one())
    tier.label = "Founding Team"
    db.flush()

    async with client_for(acme) as c:
        resp = await c.get(MINE)

    mine = next(o for o in resp.json()["organizations"]
                if o["id"] == acme.org_id)
    assert mine["tier_label"] == "Founding Team"


async def test_each_member_sees_their_OWN_tier_in_the_same_org(
    db: Session, _override_db, acme, colleague
):
    """THE SHARPEST EDGE HERE.

    Two accounts, one org, different tiers. Each must see the tier they hold —
    not the org's, not the owner's, not the first row the join happened to
    return. A bug that keyed the label lookup by org alone would pass every
    other test in this file and fail this one.
    """
    async with client_for(acme) as c:
        owner_view = await c.get(MINE)
    async with client_for(colleague) as c:
        member_view = await c.get(MINE)

    def tier_in(resp, org_id):
        return next(o for o in resp.json()["organizations"]
                    if o["id"] == org_id)["tier_key"]

    assert tier_in(owner_view, acme.org_id) == "owner"
    assert tier_in(member_view, acme.org_id) == "analyst"


async def test_a_member_is_not_told_the_orgs_other_tiers(
    db: Session, _override_db, acme, colleague
):
    """The tier MATRIX is the owner's, and it stays behind _require_owner.

    organizations/overview.py serves it and 404s everyone else. This endpoint
    may say "you are an Analyst"; it may not say what other tiers exist, who is
    on them, or which modules they open.

    Pinned as an EXACT key set rather than a list of absences: "modules is not
    in the row" only catches the leak somebody already thought of, while this
    fails on any field added to the response model without a decision. If that
    is deliberate, the failure is one line to update and a prompt to re-read
    the boundary note in mine.py.
    """
    async with client_for(colleague) as c:
        resp = await c.get(MINE)

    row = next(o for o in resp.json()["organizations"]
               if o["id"] == acme.org_id)

    assert set(row) == {
        "id", "name", "is_owner", "is_personal", "is_active",
        "tier_key", "tier_label",
    }, "a new field on this endpoint needs a look at what it discloses"

    # Their own tier, and no trace of the one the owner holds.
    assert row["tier_key"] == "analyst"
    assert "owner" not in {
        str(v).lower() for k, v in row.items() if k.startswith("tier_")
    }, "the org's other tiers are the owner's matrix, not this response"


async def test_only_orgs_the_caller_belongs_to_are_listed(
    db: Session, _override_db, acme
):
    """The account_id filter IS the boundary — see the module docstring."""
    stranger = make_tenant(db, slug="mine-beta", account_id=9603,
                           tier_key="owner", is_owner=True)

    async with client_for(acme) as c:
        resp = await c.get(MINE)

    ids = {o["id"] for o in resp.json()["organizations"]}
    assert acme.org_id in ids
    assert stranger.org_id not in ids, "an org they are not a member of"


async def test_one_membership_row_per_org(db: Session, _override_db, acme,
                                          colleague):
    """The tier join must not fan the list out.

    Joining organization_tiers without confining it to the caller's own
    (org, tier) pair would return one row per tier in the org, and the switcher
    would show the same org several times.
    """
    async with client_for(colleague) as c:
        resp = await c.get(MINE)

    ids = [o["id"] for o in resp.json()["organizations"]]
    assert len(ids) == len(set(ids)), f"duplicated orgs: {ids}"


async def test_a_tier_with_no_row_degrades_to_a_null_label(
    db: Session, _override_db, acme
):
    """tier_key is a plain string, not an FK, so the label can genuinely miss.

    It must come back null rather than 500 or silently echo the key.
    """
    db.query(OrganizationTier).filter(
        OrganizationTier.org_id == acme.org_id,
        OrganizationTier.tier_key == "owner").delete()
    db.flush()

    async with client_for(acme) as c:
        resp = await c.get(MINE)

    assert resp.status_code == 200, resp.text
    mine = next(o for o in resp.json()["organizations"]
                if o["id"] == acme.org_id)
    assert mine["tier_key"] == "owner"
    assert mine["tier_label"] is None
