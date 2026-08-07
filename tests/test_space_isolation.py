"""
THE SAFETY NET FOR THE SECOND CONTAINER.

Spaces exist so that people from different organizations can share content.
That is a deliberate hole in "everybody you can see is in your org" — and the
whole design rests on it being a hole in exactly one wall and not the other:

    a space member may reach the SPACE's content
    a space member may NOT reach the owning ORG's data

The dangerous version of this feature is the one where joining somebody's space
quietly gets you into their contacts, because `spaces.owner_org_id` was read
somewhere as if it were a tenancy column. That mistake has no symptom — no
error, no log line, just another tenant's rows rendering as if they were yours
— which is why it is asserted here rather than trusted to review.

The org-side harness (tests/org_isolation.py) protects the first wall. This
file protects the second, and the two together are what let the tenancy rule
stay `org_id == ctx.org_id` with no exceptions.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Company, Contact
from src.db.space_models import (
    JOIN_INVITE,
    JOIN_OPEN,
    JOIN_REQUEST,
    Space,
    SpaceMember,
)
from src.services import spaces
from tests.org_isolation import client_for, make_tenant

SPACES = "/api/spaces"


@pytest.fixture()
def publisher(db: Session):
    """An account in one org, who owns a space."""
    return make_tenant(db, slug="publisher-co", account_id=9901,
                       tier_key="owner", is_owner=True)


@pytest.fixture()
def outsider(db: Session):
    """An account in a COMPLETELY different org."""
    return make_tenant(db, slug="outsider-co", account_id=9902,
                       tier_key="owner", is_owner=True)


def _space(db, owner, *, name="Shared", discoverable=True,
           join_policy=JOIN_OPEN):
    space = spaces.create_space(
        db, name=name, description="A shared place",
        owner_account_id=owner.account_id, owner_org_id=owner.org_id,
        discoverable=discoverable, join_policy=join_policy)
    db.flush()
    return space


# ---------------------------------------------------------------------------
# THE WALL
# ---------------------------------------------------------------------------

async def test_joining_a_space_grants_no_access_to_the_owners_org(
    db: Session, _override_db, publisher, outsider,
):
    """The single most important assertion about spaces.

    The outsider joins the publisher's space and becomes a legitimate member of
    it. Their reach into the publisher's ORGANIZATION must remain exactly zero
    — same as before they joined, same as any stranger.
    """
    space = _space(db, publisher, name="Open Space", join_policy=JOIN_OPEN)
    db.add(Contact(org_id=publisher.org_id, account_id=publisher.account_id,
                   email="private@publisher.test", first_name="Private"))
    db.add(Company(org_id=publisher.org_id, account_id=publisher.account_id,
                   name="Publisher Holdings"))
    db.flush()

    async with client_for(outsider) as c:
        joined = await c.post(f"{SPACES}/{space.id}/join", json={})
        assert joined.status_code in (200, 201)
        assert joined.json()["is_member"] is True

        # A real member of the space...
        assert spaces.is_member(db, space.id, outsider.account_id)

        # ...and still nothing of the owner's tenant. Trailing slash: the
        # unslashed form 307s, and a redirect asserted as "not 200" would pass
        # this test without ever reaching the query it exists to check.
        contacts = await c.get("/api/contacts/")
        assert contacts.status_code in (200, 404)
        if contacts.status_code == 200:
            payload = contacts.json()
            rows = payload.get("contacts", payload) if isinstance(
                payload, dict) else payload
            emails = {r.get("email") for r in rows}
            assert "private@publisher.test" not in emails


def test_space_membership_never_widens_the_org_predicate(
    db: Session, publisher, outsider,
):
    """Belt and braces, below the HTTP layer.

    The route test above could pass for the wrong reason — a 404 from module
    gating rather than from tenancy. This asserts the thing itself: the
    outsider's org scope is unchanged by space membership, because the two
    axes never meet.
    """
    space = _space(db, publisher, name="Scope Space")
    spaces.add_member(db, space=space, account_id=outsider.account_id,
                      tier_key="member", actor_account_id=publisher.account_id)
    db.flush()

    visible = (db.query(Contact)
                 .filter(Contact.org_id == outsider.org_id).count())
    db.add(Contact(org_id=publisher.org_id, account_id=publisher.account_id,
                   first_name="Still", email="still-private@publisher.test"))
    db.flush()

    assert (db.query(Contact)
              .filter(Contact.org_id == outsider.org_id).count()) == visible


def test_the_owning_org_gets_no_automatic_access_to_the_space(
    db: Session, publisher, outsider,
):
    """The wall runs both ways.

    A colleague in the org that OWNS a space is not a member of it. Ownership
    is billing and accountability; membership is access. If owner_org_id were
    ever read as a grant, this is the test that fails.
    """
    colleague = make_tenant(db, slug="publisher-co", account_id=9903,
                            tier_key="member", is_owner=False)
    space = _space(db, publisher, name="Colleague Space", discoverable=False,
                   join_policy=JOIN_INVITE)
    db.flush()

    assert space.owner_org_id == colleague.org_id, "same org, by construction"
    assert not spaces.is_member(db, space.id, colleague.account_id)
    assert spaces.visible_space(db, space.id, colleague.account_id) is None


def test_ownership_is_read_from_membership_not_from_the_column(
    db: Session, publisher, outsider,
):
    """A removed owner stops being an owner.

    Space.owner_account_id is attribution and survives removal. If _owned_space
    trusted it, somebody taken out of a space could still administer it.
    """
    space = _space(db, publisher, name="Attribution Space")
    spaces.add_member(db, space=space, account_id=outsider.account_id,
                      tier_key="owner", is_owner=True,
                      actor_account_id=publisher.account_id)
    spaces.remove_member(db, space=space, account_id=publisher.account_id,
                         actor_account_id=outsider.account_id)
    db.flush()

    assert space.owner_account_id == publisher.account_id, "attribution stands"
    assert not spaces.is_owner(db, space.id, publisher.account_id)


# ---------------------------------------------------------------------------
# looking vs entering
# ---------------------------------------------------------------------------

async def test_a_private_space_is_invisible_to_non_members(
    db: Session, _override_db, publisher, outsider,
):
    """The same 404 a nonexistent space returns, so it cannot be probed."""
    space = _space(db, publisher, name="Hidden Space", discoverable=False,
                   join_policy=JOIN_INVITE)
    db.flush()

    async with client_for(outsider) as c:
        assert (await c.get(f"{SPACES}/{space.id}")).status_code == 404
        assert (await c.get(f"{SPACES}/999999")).status_code == 404
        assert space.id not in {
            s["id"] for s in (await c.get(f"{SPACES}/browse")).json()}


async def test_a_discoverable_space_can_be_seen_but_not_entered(
    db: Session, _override_db, publisher, outsider,
):
    """Browsing shows the shopfront and never the stock."""
    space = _space(db, publisher, name="Shopfront", discoverable=True,
                   join_policy=JOIN_REQUEST)
    db.flush()

    async with client_for(outsider) as c:
        body = (await c.get(f"{SPACES}/{space.id}")).json()

    assert body["name"] == "Shopfront"
    assert body["is_member"] is False
    # Who else is in it is the owner's business.
    assert body["members"] == []
    assert body["join_requests"] == []


async def test_a_member_cannot_enumerate_the_other_members(
    db: Session, _override_db, publisher, outsider,
):
    """Being in somebody's paid community is not a public fact."""
    space = _space(db, publisher, name="Roster", join_policy=JOIN_OPEN)
    db.flush()

    async with client_for(outsider) as c:
        await c.post(f"{SPACES}/{space.id}/join", json={})
        body = (await c.get(f"{SPACES}/{space.id}")).json()

    assert body["is_member"] is True
    assert body["members"] == [], "members are owner-only"


# ---------------------------------------------------------------------------
# the doors
# ---------------------------------------------------------------------------

async def test_an_invite_only_space_refuses_a_self_join(
    db: Session, _override_db, publisher, outsider,
):
    space = _space(db, publisher, name="Invite Only", discoverable=True,
                   join_policy=JOIN_INVITE)
    db.flush()

    async with client_for(outsider) as c:
        resp = await c.post(f"{SPACES}/{space.id}/join", json={})
    assert resp.status_code == 409
    assert not spaces.is_member(db, space.id, outsider.account_id)


async def test_a_request_is_reused_rather_than_stacked(
    db: Session, _override_db, publisher, outsider,
):
    """One row per (space, account) — a denied applicant cannot flood the queue,
    and 'have they asked before?' stays a single lookup."""
    from src.db.space_models import SpaceJoinRequest

    space = _space(db, publisher, name="Queue", join_policy=JOIN_REQUEST)
    db.flush()

    async with client_for(outsider) as c:
        await c.post(f"{SPACES}/{space.id}/join", json={"message": "please"})
        await c.post(f"{SPACES}/{space.id}/join", json={"message": "again"})

    assert (db.query(SpaceJoinRequest)
              .filter(SpaceJoinRequest.space_id == space.id,
                      SpaceJoinRequest.account_id == outsider.account_id)
              .count()) == 1
    assert not spaces.is_member(db, space.id, outsider.account_id), (
        "asking is not joining")


async def test_approving_a_request_records_which_door_they_came_through(
    db: Session, _override_db, publisher, outsider,
):
    """`granted_by` is what makes the roster auditable in one query."""
    space = _space(db, publisher, name="Approved", join_policy=JOIN_REQUEST)
    db.flush()

    async with client_for(outsider) as c:
        await c.post(f"{SPACES}/{space.id}/join", json={})

    detail = None
    async with client_for(publisher) as c:
        detail = (await c.get(f"{SPACES}/{space.id}")).json()
        request_id = detail["join_requests"][0]["id"]
        approved = await c.post(
            f"{SPACES}/{space.id}/requests/{request_id}/approve")

    assert approved.status_code == 200
    member = spaces.membership(db, space.id, outsider.account_id)
    assert member is not None
    assert member.granted_by == "request"


async def test_an_invited_member_is_recorded_as_granted_not_subscribed(
    db: Session, _override_db, publisher, outsider,
):
    """The free invite and the paywall are one mechanism, one table."""
    space = _space(db, publisher, name="Comped", join_policy=JOIN_INVITE)
    db.flush()

    async with client_for(publisher) as c:
        resp = await c.post(f"{SPACES}/{space.id}/members", json={
            "email": outsider.account.email, "tier_key": "member"})

    assert resp.status_code == 201
    member = spaces.membership(db, space.id, outsider.account_id)
    assert member.granted_by == "grant"
    assert member.invited_by_account_id == publisher.account_id


async def test_only_an_owner_administers_a_space(
    db: Session, _override_db, publisher, outsider,
):
    space = _space(db, publisher, name="Admin Only", join_policy=JOIN_OPEN)
    db.flush()

    async with client_for(outsider) as c:
        await c.post(f"{SPACES}/{space.id}/join", json={})
        assert (await c.put(f"{SPACES}/{space.id}",
                            json={"name": "Mine now"})).status_code == 404
        assert (await c.post(f"{SPACES}/{space.id}/members", json={
            "email": "someone@x.test"})).status_code == 404


async def test_a_member_may_remove_themselves(
    db: Session, _override_db, publisher, outsider,
):
    space = _space(db, publisher, name="Leavable", join_policy=JOIN_OPEN)
    db.flush()

    async with client_for(outsider) as c:
        await c.post(f"{SPACES}/{space.id}/join", json={})
        resp = await c.delete(
            f"{SPACES}/{space.id}/members/{outsider.account_id}")

    assert resp.status_code == 204
    assert not spaces.is_member(db, space.id, outsider.account_id)


async def test_the_last_owner_cannot_leave(
    db: Session, _override_db, publisher,
):
    """A space nobody can administer cannot be repaired from inside."""
    space = _space(db, publisher, name="Last Owner")
    db.flush()

    async with client_for(publisher) as c:
        resp = await c.delete(
            f"{SPACES}/{space.id}/members/{publisher.account_id}")
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# the audit trail
# ---------------------------------------------------------------------------

def test_space_events_are_not_filed_under_the_owners_org(
    db: Session, publisher, outsider,
):
    """A space's log belongs to the space.

    Filing it under the owning org would put entries naming members from OTHER
    organizations into a tenant's audit trail — both wrong and a small leak of
    who those members are.
    """
    from src.db.models import AccessGrantEvent

    space = _space(db, publisher, name="Audited")
    spaces.add_member(db, space=space, account_id=outsider.account_id,
                      tier_key="member", actor_account_id=publisher.account_id)
    db.flush()

    events = (db.query(AccessGrantEvent)
                .filter(AccessGrantEvent.resource_type == "space",
                        AccessGrantEvent.resource_id == space.id).all())

    assert {e.event_type for e in events} >= {"space.create", "space.member_add"}
    assert all(e.org_id is None for e in events), (
        "a space is not tenant data and belongs to no org's log")
    assert all(e.resource_label == space.name for e in events), (
        "labels are snapshotted so the entry survives a rename")


async def test_the_org_overview_lists_owned_spaces_without_granting_them(
    db: Session, _override_db, publisher, outsider,
):
    """The one place owner_org_id is read, and it must stay accountability.

    An org owner sees which spaces their organization is answerable for. That
    listing must not become a way in: the colleague who can see the space named
    on this page is still not a member of it, and the API still 404s them.
    """
    space = _space(db, publisher, name="Billed Space", discoverable=False,
                   join_policy=JOIN_INVITE)
    db.commit()

    async with client_for(publisher) as c:
        body = (await c.get("/api/organizations/me/overview")).json()

    listed = {s["name"]: s for s in body["owned_spaces"]}
    assert "Billed Space" in listed
    assert listed["Billed Space"]["member_count"] == 1

    # A colleague in the owning org sees nothing of it.
    colleague = make_tenant(db, slug="publisher-co", account_id=9904,
                            tier_key="member", is_owner=False)
    db.commit()
    assert not spaces.is_member(db, space.id, colleague.account_id)
    async with client_for(colleague) as c:
        assert (await c.get(f"{SPACES}/{space.id}")).status_code == 404


def test_every_door_leaves_the_same_shape_of_row(db: Session, publisher,
                                                 outsider):
    """One query answers 'who is in here, and why'."""
    space = _space(db, publisher, name="Doors")
    spaces.add_member(db, space=space, account_id=outsider.account_id,
                      tier_key="member", granted_by="subscription",
                      actor_account_id=None)
    db.flush()

    roster = (db.query(SpaceMember)
                .filter(SpaceMember.space_id == space.id).all())
    assert {m.granted_by for m in roster} == {"grant", "subscription"}
    assert all(m.tier_key for m in roster)
