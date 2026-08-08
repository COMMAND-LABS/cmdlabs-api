"""
GET /api/organizations/mine — the caller's own memberships, across orgs.

WHY THIS FILE EXISTS SEPARATELY from the other org suites: this endpoint is the
one read that is deliberately NOT org-scoped. Everywhere else
org_scope.tenant_predicate adds `org_id == ctx.org_id` behind whatever the
route asks for, so a forgotten filter still cannot cross a tenant boundary.
Here the filter on OrganizationMember.account_id is the entire boundary, with
nothing behind it — answering "which orgs am I in" across orgs is the point.

So the tests below are less about the happy path than about the SHAPE of what
comes back: the caller's own membership rows and nothing else.

WHAT CHANGED WITH ROLES. This used to return `tier_key` plus a `tier_label`
looked up from organization_tiers, and a chunk of this file guarded that lookup
— that it was filtered to the caller's own (org, tier) pairs so the org's other
tiers never entered the process, and that joining it could not fan the list out
into duplicate orgs. Roles are platform-wide constants, so there is no lookup,
no matrix to leak, and no join to duplicate rows. Those tests are gone with the
query they were protecting; the boundary tests are not.
"""
import pytest
from sqlalchemy.orm import Session

from src.config.roles_registry import ROLE_COMMUNITY_MEMBER, ROLE_MANAGER
from tests.org_isolation import client_for, make_tenant

MINE = "/api/organizations/mine"


@pytest.fixture()
def acme(db: Session):
    """An org and its owner."""
    return make_tenant(db, slug="mine-acme", account_id=9601,
                       role=ROLE_MANAGER, is_owner=True)


@pytest.fixture()
def colleague(db: Session, acme):
    """A plain member of the SAME org, in the narrower role."""
    return make_tenant(db, slug="mine-acme", account_id=9602,
                       role=ROLE_COMMUNITY_MEMBER, is_owner=False)


async def test_lists_the_callers_own_membership_with_its_role(
    db: Session, _override_db, acme
):
    async with client_for(acme) as c:
        resp = await c.get(MINE)

    assert resp.status_code == 200, resp.text
    mine = next(o for o in resp.json()["organizations"]
                if o["id"] == acme.org_id)
    assert mine["role"] == ROLE_MANAGER
    assert mine["is_owner"] is True


async def test_the_role_carries_a_display_label(db: Session, _override_db,
                                                colleague):
    """A key is an identifier; a label is what a person should be shown.

    Resolved from a constant now rather than from an org's own row, so unlike
    the tier label it replaced it can never come back null.
    """
    async with client_for(colleague) as c:
        resp = await c.get(MINE)

    mine = next(o for o in resp.json()["organizations"]
                if o["id"] == colleague.org_id)
    assert mine["role"] == ROLE_COMMUNITY_MEMBER
    assert mine["role_label"] == "Community Member"


async def test_each_member_sees_their_OWN_role_in_the_same_org(
    db: Session, _override_db, acme, colleague
):
    """THE SHARPEST EDGE HERE.

    Two accounts, one org, different roles. Each must see the role they hold —
    not the org's, not the owner's, not the first row the join happened to
    return.
    """
    async with client_for(acme) as c:
        owner_view = await c.get(MINE)
    async with client_for(colleague) as c:
        member_view = await c.get(MINE)

    def role_in(resp, org_id):
        return next(o for o in resp.json()["organizations"]
                    if o["id"] == org_id)["role"]

    assert role_in(owner_view, acme.org_id) == ROLE_MANAGER
    assert role_in(member_view, acme.org_id) == ROLE_COMMUNITY_MEMBER


async def test_the_response_carries_nothing_but_the_callers_own_membership(
    db: Session, _override_db, acme, colleague
):
    """Pinned as an EXACT key set rather than a list of absences.

    "modules is not in the row" only catches the leak somebody already thought
    of; this fails on any field added to the response model without a decision.
    If the addition is deliberate, the failure is one line to update and a
    prompt to re-read the boundary note in mine.py.
    """
    async with client_for(colleague) as c:
        resp = await c.get(MINE)

    row = next(o for o in resp.json()["organizations"]
               if o["id"] == acme.org_id)

    assert set(row) == {
        "id", "name", "is_owner", "is_personal", "is_active",
        "role", "role_label",
    }, "a new field on this endpoint needs a look at what it discloses"
    assert row["role"] == ROLE_COMMUNITY_MEMBER, "their own role, not the owner's"


async def test_only_orgs_the_caller_belongs_to_are_listed(
    db: Session, _override_db, acme
):
    """The account_id filter IS the boundary — see the module docstring."""
    stranger = make_tenant(db, slug="mine-beta", account_id=9603,
                           role=ROLE_MANAGER, is_owner=True)

    async with client_for(acme) as c:
        resp = await c.get(MINE)

    ids = {o["id"] for o in resp.json()["organizations"]}
    assert acme.org_id in ids
    assert stranger.org_id not in ids, "an org they are not a member of"


async def test_one_row_per_org(db: Session, _override_db, acme, colleague):
    """One membership, one entry.

    Cheap to keep even though the join that could fan it out is gone: the
    switcher renders this list directly, and a duplicated org there is both
    confusing and a sign something upstream started returning rows per
    something-else.
    """
    async with client_for(colleague) as c:
        resp = await c.get(MINE)

    ids = [o["id"] for o in resp.json()["organizations"]]
    assert len(ids) == len(set(ids)), f"duplicated orgs: {ids}"
