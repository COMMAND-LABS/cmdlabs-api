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
from src.db.space_models import JOIN_INVITE
from src.services import spaces
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


def _course(db, tenant, key, visibility="org", title=None, space=None):
    """A course in the tenant's ORG, or in `space` — never both.

    ck_courses_one_home enforces the exclusivity in the database; this helper
    just makes the call sites read as the choice it is.
    """
    c = Course(org_id=None if space is not None else tenant.org_id,
               space_id=space.id if space is not None else None,
               course_key=key, title=title or key.title(),
               visibility=visibility, account_id=tenant.account_id)
    db.add(c)
    db.flush()
    return c


def _space(db, owner, name):
    space = spaces.create_space(
        db, name=name, description=None, owner_account_id=owner.account_id,
        owner_org_id=owner.org_id, discoverable=False,
        join_policy=JOIN_INVITE)
    db.flush()
    return space


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
# that had already decided who was in. It is gone. Narrowing is putting the
# course in a SPACE and inviting exactly those people, which is one mechanism
# instead of two and reaches across organizations as well as inside one.

async def test_a_space_course_is_hidden_from_the_orgs_other_members(
    db: Session, _override_db, acme, student
):
    """The replacement for 'granted', and it has to be at least as narrow.

    `student` is in the same org as the space's owner and is NOT in the space.
    If an org course and a space course were reachable by the same people, the
    second container would not be narrowing anything.
    """
    space = _space(db, acme, "Cohort 3")
    _course(db, acme, "advanced", space=space)

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404
        assert (await c.get(f"{COURSES}/")).json() == []


async def test_inviting_them_to_the_space_opens_it(
    db: Session, _override_db, acme, student
):
    space = _space(db, acme, "Cohort 3")
    _course(db, acme, "advanced", space=space)

    spaces.add_member(db, space=space, account_id=student.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 200


async def test_removing_them_closes_it_on_the_next_request(
    db: Session, _override_db, acme, student
):
    space = _space(db, acme, "Cohort 3")
    _course(db, acme, "advanced", space=space)
    spaces.add_member(db, space=space, account_id=student.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 200

    spaces.remove_member(db, space=space, account_id=student.account_id)
    db.flush()

    async with client_for(student) as c:
        assert (await c.get(f"{COURSES}/advanced")).status_code == 404


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


async def test_a_space_owner_can_open_their_own_space_course(
    db: Session, _override_db, acme
):
    """They are a member of their own space, so nothing special is needed.

    This used to need an explicit owner bypass in _own_arm: an owner who marked
    a course 'granted' was not themselves a grant holder and could not open
    what they had just published. Container membership has no such gap — you
    cannot own a space without being in it.
    """
    space = _space(db, acme, "Mine")
    _course(db, acme, "narrow", space=space)
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
