"""
One home per row, and what follows from it.

A course lives in an ORG or in a SPACE. The database refuses anything else
(ck_courses_one_home), which is what keeps "who can see this?" answerable: one
container, one membership table, one answer.

The rule this file exists to protect is the reverse of the org harness's. There,
the danger is a forgotten filter letting a tenant read another tenant. Here, the
danger is the opposite mistake — a space course leaking sideways into the
ORG that happens to own the space, or an org course escaping into a space — so
both directions are asserted.
"""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Course
from src.db.space_models import JOIN_INVITE, JOIN_OPEN
from src.services import spaces
from tests.org_isolation import client_for, make_tenant

COURSES = "/api/courses"


@pytest.fixture()
def publisher(db: Session):
    return make_tenant(db, slug="home-publisher", account_id=9950,
                       tier_key="owner", is_owner=True)


@pytest.fixture()
def outsider(db: Session):
    return make_tenant(db, slug="home-outsider", account_id=9951,
                       tier_key="owner", is_owner=True)


def _space(db, owner, *, name="Shared", join_policy=JOIN_OPEN,
           discoverable=True):
    space = spaces.create_space(
        db, name=name, description=None,
        owner_account_id=owner.account_id, owner_org_id=owner.org_id,
        discoverable=discoverable, join_policy=join_policy)
    db.flush()
    return space


# ---------------------------------------------------------------------------
# the invariant itself
# ---------------------------------------------------------------------------

def test_a_course_cannot_live_in_two_places(db: Session, publisher):
    """Both homes at once is refused by the DATABASE, not by a code path.

    A check that only exists in Python is one a future writer can bypass by
    inserting from anywhere else.
    """
    space = _space(db, publisher, name="Both")
    db.add(Course(org_id=publisher.org_id, space_id=space.id,
                  course_key="both-homes", title="Both"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_a_course_cannot_be_homeless(db: Session):
    db.add(Course(org_id=None, space_id=None, course_key="nowhere",
                  title="Nowhere"))
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


# ---------------------------------------------------------------------------
# membership in the space IS the grant
# ---------------------------------------------------------------------------

async def test_a_space_member_opens_the_spaces_courses(
    db: Session, _override_db, publisher, outsider,
):
    """The payoff: content crosses org boundaries, access does not."""
    space = _space(db, publisher, name="Course Space", join_policy=JOIN_OPEN)
    db.add(Course(space_id=space.id, course_key="shared-lesson",
                  title="Shared Lesson"))
    db.commit()

    async with client_for(outsider) as c:
        # Not a member yet: the course does not exist as far as they know.
        assert (await c.get(f"{COURSES}/shared-lesson")).status_code == 404

        await c.post(f"/api/spaces/{space.id}/join", json={})

        opened = await c.get(f"{COURSES}/shared-lesson")
        assert opened.status_code == 200
        assert opened.json()["space_id"] == space.id

        listed = {row["course_key"] for row in (await c.get(f"{COURSES}/")).json()}
        assert "shared-lesson" in listed


async def test_leaving_a_space_closes_its_courses(
    db: Session, _override_db, publisher, outsider,
):
    """Access follows membership on the next request — nothing to expire."""
    space = _space(db, publisher, name="Revocable", join_policy=JOIN_OPEN)
    db.add(Course(space_id=space.id, course_key="revocable-lesson",
                  title="Revocable"))
    db.commit()

    async with client_for(outsider) as c:
        await c.post(f"/api/spaces/{space.id}/join", json={})
        assert (await c.get(f"{COURSES}/revocable-lesson")).status_code == 200

        await c.delete(f"/api/spaces/{space.id}/members/{outsider.account_id}")
        assert (await c.get(f"{COURSES}/revocable-lesson")).status_code == 404


async def test_a_space_course_is_not_reachable_through_the_owning_org(
    db: Session, _override_db, publisher,
):
    """The wall that matters most here.

    A colleague in the org that OWNS the space is not in the space, so its
    courses are not theirs. If the space arm ever consulted owner_org_id, this
    is the test that fails.
    """
    space = _space(db, publisher, name="Org Owned", discoverable=False,
                   join_policy=JOIN_INVITE)
    db.add(Course(space_id=space.id, course_key="not-yours", title="Not Yours"))
    db.commit()

    colleague = make_tenant(db, slug="home-publisher", account_id=9952,
                            tier_key="member", is_owner=False)
    db.commit()

    async with client_for(colleague) as c:
        assert (await c.get(f"{COURSES}/not-yours")).status_code == 404
        listed = {row["course_key"] for row in (await c.get(f"{COURSES}/")).json()}
        assert "not-yours" not in listed


async def test_an_org_course_does_not_leak_into_spaces(
    db: Session, _override_db, publisher, outsider,
):
    """The other direction: joining a space grants the SPACE, not the org."""
    space = _space(db, publisher, name="Leaky", join_policy=JOIN_OPEN)
    db.add(Course(org_id=publisher.org_id, course_key="org-only",
                  title="Org Only", visibility="org"))
    db.commit()

    async with client_for(outsider) as c:
        await c.post(f"/api/spaces/{space.id}/join", json={})
        assert (await c.get(f"{COURSES}/org-only")).status_code == 404


# ---------------------------------------------------------------------------
# who may put a course in a space
# ---------------------------------------------------------------------------

async def test_only_the_space_owner_adds_a_course_to_it(
    db: Session, _override_db, publisher, outsider,
):
    """Owning an ORG grants nothing over a SPACE's content, and vice versa."""
    space = _space(db, publisher, name="Guarded", join_policy=JOIN_OPEN)
    db.commit()

    async with client_for(outsider) as c:
        # A member — even an owner of their own org — cannot publish here.
        await c.post(f"/api/spaces/{space.id}/join", json={})
        refused = await c.post(f"{COURSES}/", json={
            "course_key": "intruder", "title": "Intruder",
            "space_id": space.id})
    assert refused.status_code == 404

    async with client_for(publisher) as c:
        allowed = await c.post(f"{COURSES}/", json={
            "course_key": "welcome", "title": "Welcome", "space_id": space.id})
    assert allowed.status_code == 201
    assert allowed.json()["space_id"] == space.id

    course = (db.query(Course)
                .filter(Course.course_key == "welcome").one())
    assert course.org_id is None, "a space course has no org home"


async def test_a_space_course_cannot_also_be_catalog_content(
    db: Session, _override_db, publisher,
):
    """Two access stories for one row is exactly what one-home forbids."""
    space = _space(db, publisher, name="No Catalog")
    db.commit()

    async with client_for(publisher) as c:
        resp = await c.post(f"{COURSES}/", json={
            "course_key": "confused", "title": "Confused",
            "space_id": space.id, "visibility": "catalog"})
    assert resp.status_code == 409


async def test_an_org_owner_cannot_edit_a_space_course(
    db: Session, _override_db, publisher, outsider,
):
    """Writes respect the same wall the reads do."""
    space = _space(db, publisher, name="Write Wall", join_policy=JOIN_OPEN)
    db.add(Course(space_id=space.id, course_key="theirs", title="Theirs"))
    db.commit()
    course_id = db.query(Course.id).filter(
        Course.course_key == "theirs").scalar()

    async with client_for(outsider) as c:
        await c.post(f"/api/spaces/{space.id}/join", json={})
        assert (await c.put(f"{COURSES}/{course_id}",
                            json={"title": "Mine"})).status_code == 404
        assert (await c.delete(f"{COURSES}/{course_id}")).status_code == 404

    async with client_for(publisher) as c:
        assert (await c.put(f"{COURSES}/{course_id}",
                            json={"title": "Renamed"})).status_code == 200
