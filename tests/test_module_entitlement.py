"""
Module entitlement: ceiling ∩ tier, and the three admin actions that change it.

Two levels only. TIERS ARE NOT LEVELS — each is an arbitrary set of module
keys, nothing requires one to be a superset of another, and two tiers may be
entirely disjoint. The single relationship in the system is the intersection
with the org's ceiling, which is a cap rather than a hierarchy.

Also pins the three events that had constants but no callers until now:
tier.modules_change, org.ceiling_change, and staff.join.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrantEvent,
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.main import app
from src.services import audit, modules
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import client_for, make_tenant

ENTITLEMENTS = "/api/organizations/me/entitlements"
TIERS = "/api/organizations/tiers"


@pytest.fixture()
def staff(db: Session, test_org: Organization):
    a = Account(id=8800, email="ceil@cmdlabs.io", role="admin",
                default_org_id=ROOT_ORG_ID)
    db.add(a); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=a.id,
                              tier_key="org_owner", granted_by="grant", is_owner=True))
    db.flush()
    return a


@pytest.fixture()
async def staff_client(_override_db, staff) -> AsyncClient:
    token = make_token(email=staff.email, user_id=staff.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as ac:
        yield ac


@pytest.fixture()
def acme(db: Session):
    """A client org with a ceiling and two tiers."""
    t = make_tenant(db, slug="ent-acme", account_id=8801, data_scope="shared")
    org = db.query(Organization).filter(Organization.id == t.org_id).one()
    org.granted_modules = ["home", "contacts", "companies", "deals", "agents"]

    # make_tenant already created a fully-enabled 'member' tier; narrow it
    # rather than inserting a second one (uq_org_tier_key).
    member = (db.query(OrganizationTier)
                .filter(OrganizationTier.org_id == t.org_id,
                        OrganizationTier.tier_key == "member").one())
    member.modules = ["home", "contacts"]

    # Deliberately DISJOINT from 'member' — proves tiers are sets, not levels.
    db.add(OrganizationTier(org_id=t.org_id, tier_key="analyst", label="Analyst",
                            modules=["deals", "agents"]))
    db.flush()
    return t


def _member_of(db, tenant, account_id, tier_key):
    m = make_tenant(db, slug=tenant.org.slug, account_id=account_id,
                    data_scope="shared", tier_key=tier_key, is_owner=False)
    return m


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

async def test_member_sees_only_their_tiers_modules(db: Session, _override_db, acme):
    member = _member_of(db, acme, 8802, "member")
    async with client_for(member) as c:
        body = (await c.get(ENTITLEMENTS)).json()
    assert body["modules"] == ["home", "contacts"]
    assert body["ceiling"] is None, "a non-owner has no use for the ceiling"


async def test_tiers_need_not_nest(db: Session, _override_db, acme):
    """'analyst' is not a superset of 'member' — they share nothing.

    Nothing in the model orders tiers, and this pins that: a tier is an
    arbitrary bag of modules.
    """
    analyst = _member_of(db, acme, 8803, "analyst")
    async with client_for(analyst) as c:
        body = (await c.get(ENTITLEMENTS)).json()
    # Registry order, not the order they were stored in — the menu must be
    # deterministic regardless of how a tier was edited.
    assert body["modules"] == ["agents", "deals"]
    assert "contacts" not in body["modules"]


async def test_ceiling_caps_the_tier(db: Session, _override_db, acme):
    """A tier naming a module outside the ceiling gets it silently dropped at
    read time, without the tier row being rewritten."""
    tier = (db.query(OrganizationTier)
              .filter(OrganizationTier.org_id == acme.org_id,
                      OrganizationTier.tier_key == "member").one())
    tier.modules = ["home", "contacts", "credentials"]   # credentials NOT in ceiling
    db.flush()

    member = _member_of(db, acme, 8804, "member")
    async with client_for(member) as c:
        body = (await c.get(ENTITLEMENTS)).json()
    assert "credentials" not in body["modules"]
    # The stored row is untouched — lowering a ceiling never rewrites tiers.
    assert "credentials" in db.query(OrganizationTier).filter(
        OrganizationTier.id == tier.id).one().modules


async def test_owner_gets_the_whole_ceiling(db: Session, _override_db, acme):
    """A bypass, not a stored grant: if an owner's tier could be edited down,
    one bad save would lock them out of the screen that undoes it."""
    async with client_for(acme) as c:
        body = (await c.get(ENTITLEMENTS)).json()
    assert set(body["modules"]) == {"home", "contacts", "companies", "deals", "agents"}
    assert body["ceiling"] is not None


# ---------------------------------------------------------------------------
# tier editing + audit
# ---------------------------------------------------------------------------

async def test_owner_can_set_tier_modules_and_it_is_audited(
    db: Session, _override_db, acme
):
    async with client_for(acme) as c:
        resp = await c.put(f"{TIERS}/member/modules",
                           json={"modules": ["home", "deals"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["modules"] == ["home", "deals"]

    ev = db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.TIER_MODULES_CHANGE).one()
    assert ev.org_id == acme.org_id
    assert ev.resource_label == "member"
    assert ev.detail == "home,deals"      # what it BECAME, not just "changed"


async def test_setting_a_tier_outside_the_ceiling_is_clamped(
    db: Session, _override_db, acme
):
    async with client_for(acme) as c:
        resp = await c.put(f"{TIERS}/member/modules",
                           json={"modules": ["home", "credentials", "analytics"]})
    assert resp.status_code == 200
    assert resp.json()["modules"] == ["home"], "an owner cannot exceed their ceiling"


async def test_non_owner_cannot_reach_the_tiers_surface(db: Session, _override_db, acme):
    """Read AND write, and 404 rather than 403 on both.

    The read matters as much as the write: the page returns the org's whole
    ceiling and every tier's module set, which is exactly what
    /me/entitlements deliberately withholds from non-owners. Serving it here
    would have made that restriction decorative.
    """
    member = _member_of(db, acme, 8805, "member")
    async with client_for(member) as c:
        write = await c.put(f"{TIERS}/member/modules", json={"modules": []})
        read = await c.get(TIERS)
    assert write.status_code == 404
    assert read.status_code == 404


async def test_unchanged_tier_writes_no_audit_noise(db: Session, _override_db, acme):
    """Saving the matrix without changing anything must not add a row — a log
    padded with no-ops is a log nobody reads."""
    async with client_for(acme) as c:
        await c.put(f"{TIERS}/member/modules", json={"modules": ["home", "contacts"]})
    assert db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.TIER_MODULES_CHANGE).count() == 0


# ---------------------------------------------------------------------------
# ceiling + staff join
# ---------------------------------------------------------------------------

async def test_staff_can_set_a_ceiling_and_it_is_audited(
    db: Session, _override_db, staff_client, acme
):
    resp = await staff_client.put(
        f"/api/admin/organizations/{acme.org_id}/ceiling",
        json={"modules": ["home", "agent_chat", "knowledge_bases"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["granted_modules"] == ["agent_chat", "knowledge_bases", "home"] \
        or set(resp.json()["granted_modules"]) == {"home", "agent_chat", "knowledge_bases"}

    ev = db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.ORG_CEILING_CHANGE).one()
    assert ev.org_id == acme.org_id
    assert ev.actor_email == "ceil@cmdlabs.io"
    assert ev.principal_type is None, "an org-level event has no counterparty"


async def test_lowering_a_ceiling_takes_effect_immediately(
    db: Session, _override_db, staff_client, acme
):
    """No cascade, no tier rewrite — the intersection happens at read time."""
    member = _member_of(db, acme, 8806, "member")
    async with client_for(member) as c:
        assert "contacts" in (await c.get(ENTITLEMENTS)).json()["modules"]

    await staff_client.put(f"/api/admin/organizations/{acme.org_id}/ceiling",
                           json={"modules": ["home"]})

    async with client_for(member) as c:
        assert (await c.get(ENTITLEMENTS)).json()["modules"] == ["home"]


async def test_non_staff_cannot_set_a_ceiling(db: Session, _override_db, acme):
    async with client_for(acme) as c:
        resp = await c.put(f"/api/admin/organizations/{acme.org_id}/ceiling",
                           json={"modules": []})
    assert resp.status_code == 404, "the admin surface does not confirm it exists"


async def test_staff_join_is_recorded_and_visible_to_the_org(
    db: Session, _override_db, staff_client, staff, acme
):
    """The claim customers care about: staff cannot read your data without
    appearing in your member list."""
    before = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == acme.org_id).count()

    resp = await staff_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    assert resp.status_code == 201, resp.text

    after = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == acme.org_id).all()
    assert len(after) == before + 1
    joined = next(m for m in after if m.account_id == staff.id)
    assert joined.is_owner is False, "staff join to read, not to take over"

    ev = db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.STAFF_JOIN).one()
    assert ev.org_id == acme.org_id
    assert ev.principal_label == "ceil@cmdlabs.io"


async def test_staff_join_is_idempotent(db: Session, _override_db, staff_client, acme):
    await staff_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    await staff_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    assert db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.STAFF_JOIN).count() == 1


async def test_staff_still_cannot_read_data_without_joining(
    db: Session, _override_db, staff_client, staff, acme
):
    """Staff bypass MODULES, never org_id.

    Before joining, the org's rows are simply not reachable — there is no
    filter bypass anywhere in the system.
    """
    from src.db.models import Contact
    row = Contact(org_id=acme.org_id, account_id=acme.account_id,
                  first_name="Private", last_name="X", email="p@ent.test")
    db.add(row); db.flush()

    resp = await staff_client.get("/api/contacts/")
    assert resp.status_code == 200
    assert row.id not in {c["id"] for c in resp.json()["contacts"]}
