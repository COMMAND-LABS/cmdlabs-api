"""
Reading an org named in the PATH rather than in the cookie.

WHY THESE ROUTES EXIST
----------------------
Every other org endpoint answers for the caller's ACTIVE org, resolved from a
cookie by deps.get_org_context. That is right for the dashboard, where "which
org am I working in" should be one deliberate choice. It is wrong for the
account-settings Organizations page, which shows a tab per membership: rendering
a tab must not re-scope the entire dashboard into that org.

So /organizations/{org_id}/... exists, behind deps.get_named_org_context.

WHAT THIS FILE IS REALLY GUARDING
---------------------------------
A path parameter is client input, exactly like the cookie, and the danger in
adding one is that authorization silently starts describing the WRONG ORG:

    membership   proven against the org in the PATH   (not the active one)
    ownership    proven against the org in the PATH   (not the active one)

The second is the subtle half. `OrgContext.is_owner` is the flag every admin
surface gates on, and an implementation that derived it from the active org
while serving data for the named org would let anybody who owns ANY org read
the owner's console of every org they are merely a member of. That is
test_owning_one_org_does_not_open_another_ones_console, and it is the reason
the rest of this file exists.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import OrganizationMember
from tests.org_isolation import client_for, make_tenant


def _overview(org_id: int) -> str:
    return f"/api/organizations/{org_id}/overview"


def _members(org_id: int) -> str:
    return f"/api/organizations/{org_id}/members"


@pytest.fixture()
def acme(db: Session):
    """Account 9701 OWNS Acme, and acts in it by default."""
    return make_tenant(db, slug="named-acme", account_id=9701,
                       role="manager", is_owner=True)


@pytest.fixture()
def beta(db: Session, acme):
    """A second org, owned by somebody else, that `acme`'s account JOINS.

    This is the shape the whole file turns on: one account that is an owner in
    one org and a plain member in another, acting in the first.
    """
    other = make_tenant(db, slug="named-beta", account_id=9702,
                        role="manager", is_owner=True)
    db.add(OrganizationMember(org_id=other.org_id, account_id=acme.account_id,
                              role="manager", granted_by="grant"))
    db.flush()
    return other


@pytest.fixture()
def stranger(db: Session):
    """An org our caller has nothing to do with."""
    return make_tenant(db, slug="named-stranger", account_id=9703,
                       role="manager", is_owner=True)


# ── the point of the routes ─────────────────────────────────────────────────

async def test_an_owner_reads_their_own_orgs_console_while_acting_elsewhere(
    db: Session, _override_db, acme, beta
):
    """Acting in Acme, reading Acme — but by path, not by cookie.

    The whole feature in one assertion: the data arrives without the caller
    having had to switch orgs to get it.
    """
    async with client_for(acme) as c:
        resp = await c.get(_overview(acme.org_id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == acme.org_id


async def test_a_member_reads_the_roster_of_an_org_they_are_not_acting_in(
    db: Session, _override_db, acme, beta
):
    """Acting in Acme, reading Beta's members.

    Readable because they belong to Beta — the same rule the active-org route
    applies, which is what stops the same person seeing their colleagues on one
    screen and not another.
    """
    async with client_for(acme) as c:
        resp = await c.get(_members(beta.org_id))

    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == beta.org_id
    emails = {m["email"] for m in resp.json()["members"]}
    assert len(emails) == 2, "both Beta's owner and our caller"


# ── the gates ───────────────────────────────────────────────────────────────

async def test_owning_one_org_does_not_open_another_ones_console(
    db: Session, _override_db, acme, beta
):
    """THE ASSERTION THIS FILE EXISTS FOR.

    The caller OWNS Acme and is acting in Acme, so the active context has
    is_owner=True. They are only a MEMBER of Beta. Asking for Beta's owner
    console must 404.

    An implementation that resolved ownership from the active org — or that
    kept the cookie's context and merely swapped the org id used in the queries
    — passes every other test here and fails this one.
    """
    async with client_for(acme) as c:
        resp = await c.get(_overview(beta.org_id))

    assert resp.status_code == 404, (
        f"owning Acme must not open Beta's console (got {resp.status_code})")


async def test_can_manage_describes_the_named_org_not_the_active_one(
    db: Session, _override_db, acme, beta
):
    """The same confusion, in the field the UI hides its controls behind.

    `can_manage` is what the members screen uses to show invite and remove. If
    it described the active org, a member of Beta who owns Acme would be
    offered controls that every write endpoint would then refuse.
    """
    async with client_for(acme) as c:
        mine = await c.get(_members(acme.org_id))
        theirs = await c.get(_members(beta.org_id))

    assert mine.json()["can_manage"] is True, "they own Acme"
    assert theirs.json()["can_manage"] is False, "they merely belong to Beta"


async def test_a_non_member_is_refused(db: Session, _override_db, acme,
                                       stranger):
    """The path is no more trusted than the cookie.

    Both are an org id from the client, and both go through the same membership
    check in deps._org_context_for.
    """
    async with client_for(acme) as c:
        overview = await c.get(_overview(stranger.org_id))
        members = await c.get(_members(stranger.org_id))

    assert overview.status_code == 403, overview.text
    assert members.status_code == 403, members.text


async def test_a_missing_org_looks_the_same_as_one_you_cannot_reach(
    db: Session, _override_db, acme
):
    """No enumeration oracle.

    A different status for "no such org" would let anybody walk the id space
    and learn how many organizations exist and which ids are live.
    """
    async with client_for(acme) as c:
        absent = await c.get(_members(987654))
        forbidden = await c.get(_members(acme.org_id + 100000))

    assert absent.status_code == 403
    assert forbidden.status_code == absent.status_code


# ── the active-org routes are untouched ─────────────────────────────────────

async def test_the_cookie_route_still_answers_for_the_active_org(
    db: Session, _override_db, acme, beta
):
    """/members must still describe the ACTIVE org.

    The caller can now name Beta in a path, and that must not have changed what
    the unqualified route means. This is the half of the members surface the
    Members screen still uses.

    /me/overview was the other assertion here and is gone — the route was
    removed with /dashboard/settings/organization, the only page that called
    it. Its owner-gating cases live on against the path route in
    tests/test_organization_overview.py.
    """
    async with client_for(acme) as c:
        members = await c.get("/api/organizations/members")

    assert members.status_code == 200, members.text
    assert members.json()["org_id"] == acme.org_id


async def test_the_removed_cookie_overview_route_is_gone(
    db: Session, _override_db, acme
):
    """Deleted, not quietly shadowed by the path route.

    `/me/overview` and `/{org_id}/overview` have the same shape, and org_id is
    an int — so if the literal route were ever removed while something still
    called it, FastAPI would try "me" as an org id and answer 422 rather than
    404. Pinned so the difference between "this endpoint is gone" and "this
    endpoint is broken" stays visible.
    """
    async with client_for(acme) as c:
        resp = await c.get("/api/organizations/me/overview")

    assert resp.status_code in (404, 422), resp.text
    assert resp.status_code != 200, "the route was removed"
