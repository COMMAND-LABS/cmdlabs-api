"""
Plans, and the course catalog they gate.

The rule this file exists to protect: LISTING IS NOT OPENING. A catalog is only
useful if somebody on the free plan can see what the paid one contains, and
that is exactly the shape that turns into a leak if the browse query and the
gate ever share a code path. So both are asserted on the same rows — a premium
course is listed to a free caller and refused to them in the same test.

The other rule is direction. A catalog row may only ever live in the platform
org, so "Acme publishes a course into Beta" cannot be expressed. If that check
regresses, the catalog becomes the cross-tenant channel it exists to avoid.
"""
import pytest
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.db.models import Account, Course, Organization, OrganizationMember
from tests.conftest import ROOT_ORG_ID
from tests.org_isolation import client_for, make_tenant

COURSES = "/api/courses"


def _catalog_course(db, key, plan, title=None):
    """A platform-published course, as super admins would create it."""
    course = Course(org_id=ROOT_ORG_ID, course_key=key,
                    title=title or key.replace("-", " ").title(),
                    visibility="catalog", required_plan=plan, sort_order=0)
    db.add(course)
    db.flush()
    return course


def _org_plan(db, tenant, plan):
    """Put this tenant's ORG on `plan`.

    The gate reads the ORG's plan, not the caller's account, so these fixtures
    have to say what the org has. make_tenant() pins every test org to premium
    (see the note there — an unpinned org derives from its owner and would put
    every test tenant on free), so a "free" tenant has to be pinned back down
    explicitly rather than left alone.
    """
    org = db.query(Organization).filter(Organization.id == tenant.org_id).one()
    org.pinned_plan = plan
    db.flush()
    return tenant


@pytest.fixture()
def free_user(db: Session):
    """An org on the free plan. The account's own status is beside the point."""
    return _org_plan(db, make_tenant(db, slug="plan-free-co", account_id=9801,
                                     tier_key="owner", is_owner=True),
                     plans.PLAN_FREE)


@pytest.fixture()
def premium_user(db: Session):
    t = make_tenant(db, slug="plan-paid-co", account_id=9802, tier_key="owner",
                    is_owner=True)
    account = db.query(Account).filter(Account.id == t.account_id).one()
    account.subscription_status = "active"
    db.flush()
    return _org_plan(db, t, plans.PLAN_PREMIUM)


# ---------------------------------------------------------------------------
# the plan itself
# ---------------------------------------------------------------------------

def test_a_plan_is_read_from_the_subscription_every_time(db: Session):
    """There is no stored plan, so there is nothing that can disagree.

    This test used to build an account whose `role` column said 'premium' while
    Stripe said nothing, and assert the subscription won. That drift is no
    longer expressible: the column is gone and the plan is computed on every
    read, so the only thing left to check is that the computation follows the
    status as it changes.
    """
    account = Account(id=9810, email="drifted@x.test", subscription_status=None)
    assert plans.plan_for_account(account) == plans.PLAN_FREE

    account.subscription_status = "trialing"
    assert plans.plan_for_account(account) == plans.PLAN_PREMIUM

    account.subscription_status = "canceled"
    assert plans.plan_for_account(account) == plans.PLAN_FREE

    # Super admin is a separate column and billing never touches it.
    account.is_super_admin = True
    assert plans.plan_for_account(account) == plans.PLAN_FREE


def test_both_plans_include_the_courses_module():
    """Gating the browser itself would defeat the point of a catalog."""
    for plan in plans.PLAN_KEYS:
        assert "courses" in plans.modules_for_plan(plan)


async def test_entitlements_report_the_plan(db: Session, _override_db,
                                            premium_user):
    async with client_for(premium_user) as c:
        body = (await c.get("/api/organizations/me/entitlements")).json()
    assert body["plan"] == "premium"


# ---------------------------------------------------------------------------
# listing is not opening
# ---------------------------------------------------------------------------

async def test_a_free_caller_sees_premium_courses_but_cannot_open_them(
    db: Session, _override_db, free_user,
):
    _catalog_course(db, "intro", plans.PLAN_FREE)
    _catalog_course(db, "advanced", plans.PLAN_PREMIUM)

    async with client_for(free_user) as c:
        listed = (await c.get(f"{COURSES}/")).json()
        by_key = {c_["course_key"]: c_ for c_ in listed}

        # Listed — that is the catalog doing its job.
        assert set(by_key) == {"intro", "advanced"}
        assert by_key["intro"]["locked"] is False
        assert by_key["advanced"]["locked"] is True
        assert by_key["advanced"]["required_plan"] == "premium"

        # And refused. Same rows, same request, opposite answer.
        assert (await c.get(f"{COURSES}/intro")).status_code == 200
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404


async def test_the_orgs_plan_covers_a_member_who_never_paid(
    db: Session, _override_db, premium_user,
):
    """THE PLAN BELONGS TO THE ORG, NOT TO THE ACCOUNT IN IT.

    Somebody signs up free, gets invited into a paid org, and opens what that
    org bought. Their own Stripe status is never consulted — it is the owner
    who is paying, and paying for a team that includes this person.

    This used to fail. The gate read plan_for_account(caller), so an invited
    member was refused a premium course while the module CEILING — which has
    always been the org's — let them into Contacts and Deals on the same
    request. Two containers' worth of answers for one question.
    """
    _catalog_course(db, "advanced", plans.PLAN_PREMIUM)

    invited = Account(id=9899, email="invited-free@x.com",
                      default_org_id=premium_user.org_id)
    db.add(invited)
    db.flush()
    db.add(OrganizationMember(org_id=premium_user.org_id, account_id=invited.id,
                              tier_key="owner"))
    db.flush()
    assert plans.plan_for_account(invited) == plans.PLAN_FREE, (
        "the point of the test: this account has bought nothing itself")

    from tests.org_isolation import Tenant
    async with client_for(Tenant(org=premium_user.org, account=invited)) as c:
        listed = {c_["course_key"]: c_ for c_ in (await c.get(f"{COURSES}/")).json()}
        assert listed["advanced"]["locked"] is False
        assert (await c.get(f"{COURSES}/advanced")).status_code == 200


async def test_leaving_the_paid_org_is_not_something_a_member_can_stage(
    db: Session, _override_db, free_user,
):
    """The widening has exactly one door, and the member does not hold it.

    A plan now travels with the org, so the question worth asking is whether
    anyone can put THEMSELVES in a paid one. They cannot: reaching an org at
    all requires an OrganizationMember row, and every path that writes one is
    owner-gated. Asserted here rather than argued in a comment, because "who
    can create membership" is the whole of what stops this being a hole.
    """
    other = make_tenant(db, slug="plan-paid-co-2", account_id=9898,
                        tier_key="owner", is_owner=True)
    org = db.query(Organization).filter(Organization.id == other.org_id).one()
    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()
    _catalog_course(db, "advanced", plans.PLAN_PREMIUM)

    # The free caller asks for the paid org by cookie. No membership row, so
    # the context never resolves there and the premium course stays shut.
    async with client_for(free_user) as c:
        c.cookies.set("org_id", str(other.org_id))
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404


async def test_a_premium_caller_opens_everything_unlocked(
    db: Session, _override_db, premium_user,
):
    _catalog_course(db, "intro2", plans.PLAN_FREE)
    _catalog_course(db, "advanced2", plans.PLAN_PREMIUM)

    async with client_for(premium_user) as c:
        listed = (await c.get(f"{COURSES}/")).json()
        assert all(c_["locked"] is False for c_ in listed)
        assert (await c.get(f"{COURSES}/advanced2")).status_code == 200


async def test_a_tenants_own_courses_are_never_listed_locked(
    db: Session, _override_db, free_user,
):
    """The browse arm is for the PLATFORM catalog only.

    A course another team enabled must not appear in anybody else's list at
    all — locked or otherwise. Which courses a tenant bought is theirs.
    """
    other = make_tenant(db, slug="plan-other-co", account_id=9803,
                        tier_key="owner", is_owner=True)
    db.add(Course(org_id=other.org_id, course_key="theirs", title="Theirs",
                  visibility="org", required_plan=plans.PLAN_PREMIUM))
    db.flush()

    async with client_for(free_user) as c:
        listed = (await c.get(f"{COURSES}/")).json()
    assert "theirs" not in {c_["course_key"] for c_ in listed}


# ---------------------------------------------------------------------------
# direction: only the platform publishes
# ---------------------------------------------------------------------------

async def test_an_org_owner_cannot_publish_into_the_catalog(
    db: Session, _override_db, free_user,
):
    """Otherwise the catalog becomes a cross-tenant channel."""
    async with client_for(free_user) as c:
        resp = await c.post(f"{COURSES}/", json={
            "course_key": "sneaky", "title": "Mine", "visibility": "catalog"})
    assert resp.status_code == 404
    assert db.query(Course).filter(Course.course_key == "sneaky").count() == 0


async def test_being_in_the_platform_org_is_not_enough(db: Session,
                                                       _override_db):
    """The platform org is also where the public signs up.

    An org check alone would let any of those accounts publish courseware into
    every tenant — the same two-condition rule catalog.assert_publishable uses.
    """
    # A member of the platform org who is an OWNER there but is not a super
    # admin — the case an org-only check would wave through.
    signup = make_tenant(db, slug="root", account_id=9804, tier_key="owner",
                         is_owner=True)
    async with client_for(signup) as c:
        resp = await c.post(f"{COURSES}/", json={
            "course_key": "not-super-admin", "title": "X", "visibility": "catalog"})
    assert resp.status_code == 404


async def test_a_non_super_admin_owner_cannot_edit_a_published_course(
    db: Session, _override_db, free_user,
):
    """The guard covers the update path too, not only create."""
    course = _catalog_course(db, "locked-down", plans.PLAN_PREMIUM)
    async with client_for(free_user) as c:
        resp = await c.put(f"{COURSES}/{course.id}",
                           json={"required_plan": "free"})
    assert resp.status_code == 404
    db.refresh(course)
    assert course.required_plan == "premium"
