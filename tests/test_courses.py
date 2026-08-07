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
    c = Course(org_id=tenant.org_id, course_key=key, title=title or key.title(),
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
# the grant arm
# ---------------------------------------------------------------------------

async def test_a_granted_course_is_hidden_until_granted(
    db: Session, _override_db, acme, student
):
    _course(db, acme, "advanced", visibility="granted")
    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404
        assert (await c.get(f"{COURSES}/")).json() == []


async def test_a_grant_opens_it_for_the_person_named(
    db: Session, _override_db, acme, student
):
    """One grant, one person.

    This used to be "a group grant opens it for that department". Groups are
    spaces now, and reaching a SET of people means putting the course in a
    space (courses.space_id) rather than naming a set from a grant row.
    """
    course = _course(db, acme, "advanced", visibility="granted")

    async with client_for(acme) as c:
        granted = await c.post(f"{COURSES}/{course.id}/access-grants",
                               json={"granteeEmail": student.account.email})
    assert granted.status_code == 201, granted.text

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 200


async def test_revoking_closes_it_on_the_next_request(
    db: Session, _override_db, acme, student
):
    course = _course(db, acme, "advanced", visibility="granted")
    async with client_for(acme) as c:
        grant = await c.post(f"{COURSES}/{course.id}/access-grants",
                             json={"granteeEmail": student.account.email})
        grant_id = grant.json()["id"]

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 200

    async with client_for(acme) as c:
        assert (await c.delete(
            f"{COURSES}/{course.id}/access-grants/{grant_id}")).status_code == 204

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404


async def test_granting_an_org_wide_course_is_refused(
    db: Session, _override_db, acme, student
):
    """A row that changes nothing reads like access without being it."""
    course = _course(db, acme, "open-to-all", visibility="org")
    async with client_for(acme) as c:
        resp = await c.post(f"{COURSES}/{course.id}/access-grants",
                            json={"granteeEmail": student.account.email})
    assert resp.status_code == 409


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


async def test_an_owner_sees_their_own_granted_course(db: Session, _override_db, acme):
    """Otherwise they could publish a narrow course and not be able to open it
    to check what they just published."""
    _course(db, acme, "narrow", visibility="granted")
    async with client_for(acme) as c:
        assert (await c.get(f"{COURSES}/narrow")).status_code == 200


async def test_deleting_a_course_revokes_its_grants(db: Session, _override_db,
                                                    acme, student):
    from src.db.models import AccessGrant

    course = _course(db, acme, "temp", visibility="granted")
    async with client_for(acme) as c:
        await c.post(f"{COURSES}/{course.id}/access-grants",
                     json={"granteeEmail": student.account.email})
        assert (await c.delete(f"{COURSES}/{course.id}")).status_code == 204

    assert db.query(AccessGrant).filter(
        AccessGrant.resource_type == "course").count() == 0


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
