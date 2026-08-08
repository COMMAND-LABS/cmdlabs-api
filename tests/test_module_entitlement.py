"""
Module entitlement: ceiling ∩ role, and the admin actions that change it.

Two levels only, and the right-hand one is now a CONSTANT. It used to be
organization_tiers — an arbitrary per-org set of module keys the owner edited,
where nothing required one tier to be a superset of another and two could be
entirely disjoint. A whole section of this file tested that matrix: setting a
tier's modules, clamping a save to the ceiling, refusing the surface to
non-owners, and not writing audit noise on an unchanged save. All of it went
with the table.

What survives is the part that was never about tiers: the CAP. A role can only
ever open what the org's plan allows, the owner bypasses the role layer, and a
super admin bypasses both.

Also pins the events that have constants but few callers: org.ceiling_change
and super_admin.join.
"""
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrantEvent,
    Account,
    Organization,
    OrganizationMember,
)
from src.config import plans_registry as plans
from src.config.modules_registry import MODULE_KEYS
from src.config.roles_registry import (
    COMMUNITY_MODULES, ROLE_COMMUNITY_MEMBER, ROLE_MANAGER,
)
from src.deps import OrgContext
from src.main import app
from src.services import audit, modules
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import client_for, make_tenant

ENTITLEMENTS = "/api/organizations/me/entitlements"


@pytest.fixture()
def super_admin(db: Session, test_org: Organization):
    a = Account(id=8800, email="ceil@cmdlabs.io", is_super_admin=True,
                default_org_id=ROOT_ORG_ID)
    db.add(a); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=a.id,
                              role="manager", granted_by="grant"))
    db.flush()
    return a


@pytest.fixture()
async def super_admin_client(_override_db, super_admin) -> AsyncClient:
    token = make_token(email=super_admin.email, user_id=super_admin.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as ac:
        yield ac


@pytest.fixture()
def acme(db: Session):
    """A client org on the premium plan.

    Nothing per-org to configure any more: roles are constants, so this is just
    an org with a plan.
    """
    t = make_tenant(db, slug="ent-acme", account_id=8801, data_scope="shared")
    org = db.query(Organization).filter(Organization.id == t.org_id).one()
    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()
    return t


def _member_of(db, tenant, account_id, role):
    return make_tenant(db, slug=tenant.org.name.lower().replace(" ", "-"),
                       account_id=account_id, data_scope="shared",
                       role=role, is_owner=False)


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

async def test_a_community_member_sees_only_the_allowlist(
    db: Session, _override_db, acme
):
    """The narrow role, on a PREMIUM org.

    The plan is irrelevant to what they get — that is the point of the role.
    An org paying for the full surface still shows a community member three
    screens.
    """
    member = _member_of(db, acme, 8802, ROLE_COMMUNITY_MEMBER)
    async with client_for(member) as c:
        body = (await c.get(ENTITLEMENTS)).json()

    assert set(body["modules"]) == set(COMMUNITY_MODULES)
    assert body["ceiling"] is None, "a non-owner has no use for the ceiling"


async def test_a_community_member_reaches_no_crm_module(
    db: Session, _override_db, acme
):
    """THE ASSERTION THE ROLE EXISTS FOR.

    Named separately from the set-equality above because this is the property
    somebody would break by "just adding one module" to COMMUNITY_MODULES. The
    set test would be updated to match without anybody noticing what it meant;
    this one says out loud what may not happen.

    Note the ceiling in force is PREMIUM, so every one of these is bought and
    available — the role is the only thing withholding them.
    """
    member = _member_of(db, acme, 8806, ROLE_COMMUNITY_MEMBER)
    async with client_for(member) as c:
        body = (await c.get(ENTITLEMENTS)).json()

    for key in ("contacts", "contact_lists", "companies", "deals",
                "credentials", "access", "analytics", "email_campaigns"):
        assert key not in body["modules"], (
            f"a community member must not reach {key}")


async def test_a_manager_tracks_the_whole_plan(db: Session, _override_db, acme):
    """A manager is the ceiling, not a list.

    Written as an equality against the plan rather than a fixed set of keys, so
    that adding a module to PLAN_PREMIUM tomorrow keeps this passing WITHOUT an
    edit — which is the behaviour being asserted. A snapshot here would pass
    while the product silently stopped giving managers new modules, the exact
    bug plans_registry records about frozen module lists.
    """
    manager = _member_of(db, acme, 8803, ROLE_MANAGER)
    async with client_for(manager) as c:
        body = (await c.get(ENTITLEMENTS)).json()

    assert set(body["modules"]) == set(
        plans.modules_for_plan(plans.PLAN_PREMIUM))


async def test_the_ceiling_caps_a_manager(db: Session, _override_db):
    """A role can never open what the org did not buy.

    On a FREE org a manager gets the free plan, not the premium surface their
    role would allow on a richer plan. The cap runs one way and this is it.
    """
    free_org = make_tenant(db, slug="ent-free", account_id=8807,
                           data_scope="shared")
    org = db.query(Organization).filter(Organization.id == free_org.org_id).one()
    org.pinned_plan = plans.PLAN_FREE
    db.flush()

    manager = _member_of(db, free_org, 8808, ROLE_MANAGER)
    async with client_for(manager) as c:
        body = (await c.get(ENTITLEMENTS)).json()

    assert set(body["modules"]) == set(plans.modules_for_plan(plans.PLAN_FREE))
    assert "contacts" not in body["modules"], "not on the free plan"


async def test_owner_gets_the_whole_ceiling_whatever_their_role(
    db: Session, _override_db, acme
):
    """A bypass, not a stored grant.

    The owner's membership row says community_member — every row does after the
    roles migration — and they still get everything. If this ever stops being
    true, an owner could be locked out of their own org by the value in a column
    that is supposed to be inert for them.
    """
    member = (db.query(OrganizationMember)
                .filter(OrganizationMember.org_id == acme.org_id,
                        OrganizationMember.account_id == acme.account_id).one())
    member.role = ROLE_COMMUNITY_MEMBER
    db.flush()

    async with client_for(acme) as c:
        body = (await c.get(ENTITLEMENTS)).json()

    assert set(body["modules"]) == set(
        plans.modules_for_plan(plans.PLAN_PREMIUM))
    assert body["ceiling"] is not None


# ---------------------------------------------------------------------------
# ceiling + super admin join
# ---------------------------------------------------------------------------

async def test_super_admin_can_pin_a_plan_and_it_is_audited(
    db: Session, _override_db, super_admin_client, acme
):
    resp = await super_admin_client.put(
        f"/api/admin/organizations/{acme.org_id}/plan",
        json={"plan": "free"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["pinned_plan"] == "free"
    assert resp.json()["modules"] == plans.modules_for_plan(plans.PLAN_FREE), (
        "the response resolves the plan so the caller need not")

    ev = db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.ORG_CEILING_CHANGE).one()
    assert ev.org_id == acme.org_id
    assert ev.actor_email == "ceil@cmdlabs.io"
    assert ev.principal_type is None, "an org-level event has no counterparty"


async def test_lowering_a_ceiling_takes_effect_immediately(
    db: Session, _override_db, super_admin_client, acme
):
    """No cascade, no rewrite anywhere — the intersection happens at read time."""
    member = _member_of(db, acme, 8806, ROLE_MANAGER)
    async with client_for(member) as c:
        assert "contacts" in (await c.get(ENTITLEMENTS)).json()["modules"]

    await super_admin_client.put(f"/api/admin/organizations/{acme.org_id}/plan",
                           json={"plan": "free"})

    async with client_for(member) as c:
        # The manager role still reaches for everything; the free ceiling no
        # longer includes contacts, and the cap is applied per request.
        assert "contacts" not in (await c.get(ENTITLEMENTS)).json()["modules"]


async def test_non_super_admin_cannot_set_a_plan(db: Session, _override_db, acme):
    async with client_for(acme) as c:
        resp = await c.put(f"/api/admin/organizations/{acme.org_id}/plan",
                           json={"plan": "premium"})
    assert resp.status_code == 404, "the admin surface does not confirm it exists"


async def test_super_admin_join_is_recorded_and_visible_to_the_org(
    db: Session, _override_db, super_admin_client, super_admin, acme
):
    """The claim customers care about: super admins cannot read your data
    without
    appearing in your member list."""
    before = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == acme.org_id).count()

    resp = await super_admin_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    assert resp.status_code == 201, resp.text

    after = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == acme.org_id).all()
    assert len(after) == before + 1
    next(m for m in after if m.account_id == super_admin.id)
    org = db.query(Organization).filter(Organization.id == acme.org_id).one()
    assert org.owner_account_id != super_admin.id, (
        "super admins join to read, not to take over — joining adds a "
        "membership row and never touches who the org says owns it")

    ev = db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.SUPER_ADMIN_JOIN).one()
    assert ev.org_id == acme.org_id
    assert ev.principal_label == "ceil@cmdlabs.io"


async def test_super_admin_join_is_idempotent(db: Session, _override_db, super_admin_client, acme):
    await super_admin_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    await super_admin_client.post(f"/api/admin/organizations/{acme.org_id}/join")
    assert db.query(AccessGrantEvent).filter(
        AccessGrantEvent.event_type == audit.SUPER_ADMIN_JOIN).count() == 1


async def test_super_admin_still_cannot_read_data_without_joining(
    db: Session, _override_db, super_admin_client, super_admin, acme
):
    """Super admins bypass MODULES, never org_id.

    Before joining, the org's rows are simply not reachable — there is no
    filter bypass anywhere in the system.
    """
    from src.db.models import Contact
    row = Contact(org_id=acme.org_id, account_id=acme.account_id,
                  first_name="Private", last_name="X", email="p@ent.test")
    db.add(row); db.flush()

    resp = await super_admin_client.get("/api/contacts/")
    assert resp.status_code == 200
    assert row.id not in {c["id"] for c in resp.json()["contacts"]}


def test_super_admin_are_never_the_last_to_open_a_new_module(db: Session, test_org):
    """A plan can be narrower than the registry; super admins must not be.

    Two module keys are in no plan at all (membership, organization), and any
    new one starts that way until it is added — which is how `courses` and
    `courses` once shipped invisible to super admins. Fixed by removing the
    cap for super admins rather than by keeping something in step with the
    registry.
    """
    narrow = Organization(name="Stale", pinned_plan=plans.PLAN_FREE)
    db.add(narrow)
    db.flush()

    super_admin = OrgContext(account_id=1, org_id=narrow.id, role="manager", is_super_admin=True)
    assert modules.effective_modules(db, super_admin) == list(MODULE_KEYS)


def test_a_tenant_ceiling_is_still_exactly_its_plan(db: Session):
    """The super admin rule must not leak into anybody else's org.

    If this ever equals the full registry, the bypass above has stopped being
    special and every tenant has silently been given everything.
    """
    tenant = make_tenant(db, slug="ceiling-tenant", account_id=8890,
                         role="manager", is_owner=True)
    org = db.query(Organization).filter(Organization.id == tenant.org_id).one()
    org.pinned_plan = plans.PLAN_FREE
    db.flush()

    assert modules.ceiling_for(db, tenant.org_id) == plans.modules_for_plan(
        plans.PLAN_FREE)
    assert len(modules.ceiling_for(db, tenant.org_id)) < len(MODULE_KEYS)


def test_a_non_super_admin_member_of_the_platform_org_is_still_capped_by_their_role(
    db: Session, test_org,
):
    """The safety argument for the rule above, asserted rather than assumed.

    Root still holds at least one account from when it was the public lobby.
    Widening its ceiling must not widen them: they are not an owner and not a
    super admin, so their modules are ceiling n role, and the role is what
    holds.
    """
    stray = make_tenant(db, slug="root", account_id=8891,
                        role=ROLE_COMMUNITY_MEMBER, is_owner=False)

    ctx = OrgContext(account_id=stray.account_id, org_id=test_org.id,
                     role=ROLE_COMMUNITY_MEMBER, is_super_admin=False)
    resolved = modules.effective_modules(db, ctx)

    assert set(resolved) <= set(COMMUNITY_MODULES)
    assert "contacts" not in resolved, (
        "a full ceiling on the platform org must not reach a community member")
