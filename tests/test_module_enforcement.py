"""
Module gating is enforced on the server, not just in the menu.

Until this landed, a tier that excluded Deals only hid the sidebar item —
anyone who typed /api/deals reached the data anyway. That is precisely the
state cmdlabs-ui/src/config/roles.ts documents about the pre-org system in its
own header comment, and it is what makes the difference between an
authorization boundary and a cosmetic one.

Also asserts every registered router prefix is DELIBERATELY classified, so
adding a router without deciding whether it is gated fails here rather than
shipping ungated.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.config.modules_registry import (
    ALWAYS_ALLOWED_PREFIXES,
    MODULE_KEYS,
    module_for_path,
)
from src.config.roles_registry import ROLE_COMMUNITY_MEMBER
from src.db.models import (
    Account, Agent, Contact, Organization, OrganizationMember,
)
from src.main import _ROUTERS
from tests.org_isolation import client_for, make_tenant


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="enf-acme", account_id=9101, data_scope="shared",
                       role="manager", is_owner=False)


def _narrow_to_community(db, tenant):
    """Put this tenant's member in the narrow role.

    Replaces _narrow_tier_to(db, tenant, modules), which edited a tier row to an
    arbitrary module set. A role cannot be narrowed to an arbitrary set — that
    is the point of them — so enforcement is now asserted against the smallest
    role the platform has.
    """
    member = (db.query(OrganizationMember)
                .filter(OrganizationMember.org_id == tenant.org_id,
                        OrganizationMember.account_id == tenant.account_id).one())
    member.role = ROLE_COMMUNITY_MEMBER
    db.flush()


# ---------------------------------------------------------------------------
# the registry covers everything that is mounted
# ---------------------------------------------------------------------------

def test_every_router_prefix_is_classified():
    """A new router must either map to a module or be explicitly always-allowed.

    Without this, adding a router silently ships it ungated — and nothing else
    in the suite would notice, because its own tests would pass.
    """
    unclassified = []
    for _router, prefix, _tags in _ROUTERS:
        if not prefix:
            continue
        if module_for_path(prefix) is None and not any(
            prefix.startswith(p) for p in ALWAYS_ALLOWED_PREFIXES
        ):
            unclassified.append(prefix)

    assert not unclassified, (
        "These router prefixes are neither gated by a module nor listed as "
        f"always-allowed: {unclassified}\n"
        "Add route_prefixes to a Module in src/config/modules_registry.py, or "
        "add the prefix to ALWAYS_ALLOWED_PREFIXES with a reason."
    )


def test_auth_and_billing_are_never_gated():
    """Gating these would be self-defeating: an account with no modules could
    not sign in, see its settings, or pay to get more."""
    for prefix in ("/api/auth", "/api/accounts", "/api/billing",
                   "/api/organizations"):
        assert module_for_path(prefix) is None, f"{prefix} must not be gated"


def test_longest_prefix_wins():
    """A more specific route must not be shadowed by a shorter one that shares
    its opening segments."""
    assert module_for_path("/api/contact-lists").key == "contact_lists"
    assert module_for_path("/api/contacts").key == "contacts"


# ---------------------------------------------------------------------------
# enforcement
# ---------------------------------------------------------------------------

async def test_excluded_module_is_refused(db: Session, _override_db, acme):
    """One module in the role's allowlist, one outside it.

    Both are inside the ORG's ceiling — the org is on premium — so the role is
    the only thing refusing the second. That is the property under test: the
    gate is the role, not the plan.
    """
    _narrow_to_community(db, acme)

    async with client_for(acme) as c:
        allowed = await c.get("/api/courses/")     # in COMMUNITY_MODULES
        denied = await c.get("/api/deals/")        # deliberately not

    assert allowed.status_code == 200
    assert denied.status_code == 404, (
        "a module outside the caller's role must not be reachable by URL")


async def test_denial_is_404_not_403(db: Session, _override_db, acme):
    """404 so a paid feature does not advertise its own existence to someone
    who cannot use it."""
    _narrow_to_community(db, acme)
    async with client_for(acme) as c:
        resp = await c.get("/api/deals/")
    assert resp.status_code == 404
    assert "deals" not in resp.text.lower()


async def test_writes_are_refused_too(db: Session, _override_db, acme):
    """A gated read with an ungated write is still a hole — the caller cannot
    list deals but could create one."""
    _narrow_to_community(db, acme)
    async with client_for(acme) as c:
        resp = await c.post("/api/deals/", json={"title": "Sneaky"})
    assert resp.status_code == 404


async def test_narrowing_the_plan_revokes_immediately(
    db: Session, _override_db, acme
):
    """No cascade, no re-login: the intersection happens per request."""
    async with client_for(acme) as c:
        assert (await c.get("/api/deals/")).status_code == 200

    org = db.query(Organization).filter(Organization.id == acme.org_id).one()
    org.pinned_plan = "free"      # the free plan sells no CRM
    db.flush()

    async with client_for(acme) as c:
        assert (await c.get("/api/deals/")).status_code == 404


async def test_owner_reaches_everything_in_the_ceiling(db: Session, _override_db):
    """An owner bypasses their role.

    Given the SMALLEST role explicitly, which is also what the roles migration
    left every row holding. If the bypass ever went away, every owner on the
    platform would be locked out of their own org at once.
    """
    owner = make_tenant(db, slug="enf-owner", account_id=9102,
                        data_scope="shared", role=ROLE_COMMUNITY_MEMBER,
                        is_owner=True)

    async with client_for(owner) as c:
        assert (await c.get("/api/deals/")).status_code == 200


# ---------------------------------------------------------------------------
# read-only orgs
# ---------------------------------------------------------------------------

async def test_a_lapsed_org_can_read_but_not_write(db: Session, _override_db, acme):
    """The grace window: keep every screen, refuse every change.

    Set up by dating the OWNER's lapse, because that is the only thing stored.
    There is no org.status to flip — a column that nothing ever wrote is what
    this replaced — so a test that could still flip one would be testing a
    fiction.

    Uses AGENTS with a MANAGER, so the viewer definitely has the module and
    this asserts on the read-only window rather than accidentally testing
    entitlement.
    """
    org = db.query(Organization).filter(Organization.id == acme.org_id).one()
    owner = db.query(Account).filter(Account.id == acme.account_id).one()
    owner.subscription_status = "canceled"
    owner.subscription_lapsed_at = datetime.now(timezone.utc) - timedelta(days=1)
    org.owner_account_id = owner.id
    org.pinned_plan = None       # follow the owner's billing, which just lapsed
    db.flush()

    kept = Agent(org_id=acme.org_id, account_id=acme.account_id,
                 name="Kept", visibility="org", config={"data": {}})
    db.add(kept); db.flush()

    async with client_for(acme) as c:
        read = await c.get("/api/agents/")
        write = await c.post("/api/agents/", json={
            "name": "New", "config": {"data": {}}})

    # Everything still opens. Their data has not gone anywhere, and that is the
    # entire message of this state.
    assert read.status_code == 200, read.text
    assert kept.id in {int(a["id"]) for a in read.json()}
    # Nothing may be changed.
    assert write.status_code == 403, write.text
    assert "read-only" in write.json()["detail"].lower()
