"""
Course access.

The content is code — routes in the Next.js app — so nothing here tests
rendering. What is tested is the only question the server can answer and the UI
cannot: may THIS caller open THAT course, in the org they are currently acting
in.

That question has to be answered server-side. The dashboard's own
canAccessPath() runs in a client effect and redirects, so the page renders and
streams first; for paid courseware that is a content leak on every load rather
than a gate. GET /api/courses/{course_key} is what a course's server component
calls instead, and these tests pin its behaviour.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Course, OrganizationTier
from tests.org_isolation import client_for, make_tenant

COURSES = "/api/courses"


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="course-co", account_id=9801, tier_key="owner",
                       is_owner=True)


@pytest.fixture()
def student(db: Session, acme):
    """A plain member of the same org."""
    return make_tenant(db, slug="course-co", account_id=9802,
                       tier_key="member", is_owner=False)


def _course(db, tenant, key, visibility="org", title=None):
    """A course in the tenant's ORG — the only home there is.

    It took a `space=` argument while courses were dual-homed, and the database
    refused anything with both homes or neither (ck_courses_one_home). org_id is
    plain NOT NULL now.
    """
    c = Course(org_id=tenant.org_id,
               course_key=key, title=title or key.title(),
               visibility=visibility, account_id=tenant.account_id)
    db.add(c)
    db.flush()
    return c


# ---------------------------------------------------------------------------
# the org arm
# ---------------------------------------------------------------------------

async def test_every_member_sees_an_org_wide_course(db: Session, _override_db,
                                                    acme, student):
    _course(db, acme, "bsop-intro")
    async with client_for(student) as c:
        listed = await c.get(f"{COURSES}/")
        gate = await c.get(f"{COURSES}/bsop-intro")
    assert [x["course_key"] for x in listed.json()] == ["bsop-intro"]
    assert gate.status_code == 200


async def test_a_course_never_crosses_orgs(db: Session, _override_db, acme):
    """The tenancy boundary, on the one table whose content is shared code.

    Two orgs may hold the same course_key and render the same route — that is
    the point, and it moves no tenant data. What must never happen is one org
    reaching the other's ENABLEMENT row, because that is what carries the
    title, the ordering, and the grants.
    """
    outsider = make_tenant(db, slug="course-outsider", account_id=9803)
    _course(db, acme, "bsop-intro")

    async with client_for(outsider) as c:
        listed = await c.get(f"{COURSES}/")
        gate = await c.get(f"{COURSES}/bsop-intro")
    assert listed.json() == []
    assert gate.status_code == 404


async def test_the_same_key_in_two_orgs_is_two_enablements(
    db: Session, _override_db, acme
):
    """One course in code, enabled independently. Neither org can see or
    affect the other's row."""
    other = make_tenant(db, slug="course-two", account_id=9804, tier_key="owner",
                        is_owner=True)
    mine = _course(db, acme, "shared-key", title="Ours")
    theirs = _course(db, other, "shared-key", title="Theirs")
    assert mine.id != theirs.id

    async with client_for(acme) as c:
        assert (await c.get(f"{COURSES}/shared-key")).json()["title"] == "Ours"
    async with client_for(other) as c:
        assert (await c.get(f"{COURSES}/shared-key")).json()["title"] == "Theirs"


# ---------------------------------------------------------------------------
# narrowing a course to SOME people
# ---------------------------------------------------------------------------
#
# There used to be a third visibility, 'granted', plus AccessGrant rows naming
# individual accounts — a per-course permission on top of the org membership
# that had already decided who was in. It is gone, and its replacement went too:
# narrowing was putting the course in a SPACE and inviting exactly those people,
# one mechanism instead of two, reaching across organizations as well as inside
# one.
#
# So narrowing is CURRENTLY NOT POSSIBLE — an org's course is open to the whole
# org. Three tests covered the replacement and were deleted with spaces: that a
# space course was hidden from the org's other members (it had to be at least as
# narrow as 'granted' to be a replacement at all), that inviting someone opened
# it, and that removing them closed it on the next request. Restore all three
# with whatever narrowing mechanism arrives; the first is the one that matters.

async def test_a_course_can_no_longer_be_marked_granted(
    db: Session, _override_db, acme
):
    """The vocabulary is closed, and the API is where that is asserted.

    A client still sending the old value gets a validation error rather than a
    row the read path would never match — which is what it would have become
    if only the CHECK constraint had been narrowed.
    """
    async with client_for(acme) as c:
        resp = await c.post(f"{COURSES}/", json={
            "course_key": "narrow", "title": "Narrow",
            "visibility": "granted"})
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# managing them
# ---------------------------------------------------------------------------

async def test_only_an_owner_enables_a_course(db: Session, _override_db,
                                              acme, student):
    async with client_for(student) as c:
        resp = await c.post(f"{COURSES}/", json={"course_key": "sneaky-course",
                                                 "title": "X"})
    assert resp.status_code == 404


async def test_course_keys_are_validated_and_normalized(
    db: Session, _override_db, acme
):
    async with client_for(acme) as c:
        bad = await c.post(f"{COURSES}/", json={"course_key": "Not A Key",
                                                "title": "X"})
        ok = await c.post(f"{COURSES}/", json={"course_key": "BSOP-Intro",
                                               "title": "Intro"})
    assert bad.status_code == 422
    assert ok.json()["course_key"] == "bsop-intro"


async def test_enabling_twice_is_a_conflict(db: Session, _override_db, acme):
    async with client_for(acme) as c:
        await c.post(f"{COURSES}/", json={"course_key": "dup", "title": "A"})
        again = await c.post(f"{COURSES}/", json={"course_key": "dup",
                                                  "title": "B"})
    assert again.status_code == 409


async def test_the_key_cannot_be_edited(db: Session, _override_db, acme):
    """It is the identifier grants are written against, so changing it would
    revoke access silently. Retitling stays free."""
    course = _course(db, acme, "stable-key", title="Before")
    async with client_for(acme) as c:
        resp = await c.put(f"{COURSES}/{course.id}",
                           json={"title": "After", "course_key": "new-key"})
    assert resp.status_code == 200
    assert resp.json()["title"] == "After"
    assert resp.json()["course_key"] == "stable-key"


async def test_an_owner_can_open_their_own_orgs_course(
    db: Session, _override_db, acme
):
    """No owner bypass needed, and that is the point.

    _own_arm used to carry an explicit one: an owner who marked a course
    'granted' was not themselves a grant holder and could not open what they had
    just published. Container membership has no such gap — you cannot own a
    container without being in it.
    """
    _course(db, acme, "narrow")
    async with client_for(acme) as c:
        assert (await c.get(f"{COURSES}/narrow")).status_code == 200


async def test_deleting_a_course_needs_no_cascade(db: Session, _override_db,
                                                  acme):
    """A course carries no grants of its own, so the row IS the revocation."""
    course = _course(db, acme, "temp")
    async with client_for(acme) as c:
        assert (await c.delete(f"{COURSES}/{course.id}")).status_code == 204

    assert db.query(Course).filter(Course.id == course.id).first() is None


# ---------------------------------------------------------------------------
# module gating
# ---------------------------------------------------------------------------

async def test_a_tier_without_the_courses_module_reaches_nothing(
    db: Session, _override_db, acme, student
):
    """Banding free vs paid courseware is a TIER question, and this is the
    lever — module keys are platform-wide, unlike tier keys."""
    _course(db, acme, "bsop-intro")
    tier = (db.query(OrganizationTier)
              .filter(OrganizationTier.org_id == acme.org_id,
                      OrganizationTier.tier_key == "member").one())
    tier.modules = [m for m in tier.modules if m != "courses"]
    db.flush()

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/bsop-intro")).status_code == 404
        assert (await c.get(f"{COURSES}/")).status_code == 404
