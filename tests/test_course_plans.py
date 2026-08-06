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
from src.db.models import Account, Course
from tests.conftest import ROOT_ORG_ID
from tests.org_isolation import client_for, make_tenant

COURSES = "/api/courses"


def _catalog_course(db, key, plan, title=None):
    """A platform-published course, as staff would create it."""
    course = Course(org_id=ROOT_ORG_ID, course_key=key,
                    title=title or key.replace("-", " ").title(),
                    visibility="catalog", required_plan=plan, sort_order=0)
    db.add(course)
    db.flush()
    return course


@pytest.fixture()
def free_user(db: Session):
    t = make_tenant(db, slug="plan-free-co", account_id=9801, tier_key="owner",
                    is_owner=True)
    return t


@pytest.fixture()
def premium_user(db: Session):
    t = make_tenant(db, slug="plan-paid-co", account_id=9802, tier_key="owner",
                    is_owner=True)
    account = db.query(Account).filter(Account.id == t.account_id).one()
    account.subscription_status = "active"
    db.flush()
    return t


# ---------------------------------------------------------------------------
# the plan itself
# ---------------------------------------------------------------------------

def test_a_plan_is_the_subscription_not_the_role(db: Session):
    """accounts.role is a cache of Stripe; the subscription is the fact."""
    account = Account(id=9810, email="drifted@x.test", role="premium",
                      subscription_status=None)
    assert plans.plan_for_account(account) == plans.PLAN_FREE

    account.subscription_status = "trialing"
    assert plans.plan_for_account(account) == plans.PLAN_PREMIUM


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
    # A member of the platform org who is an OWNER there but is not staff —
    # the case an org-only check would wave through.
    signup = make_tenant(db, slug="root", account_id=9804, tier_key="owner",
                         is_owner=True)
    async with client_for(signup) as c:
        resp = await c.post(f"{COURSES}/", json={
            "course_key": "not-staff", "title": "X", "visibility": "catalog"})
    assert resp.status_code == 404


async def test_a_non_staff_owner_cannot_edit_a_published_course(
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
