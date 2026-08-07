"""
Agents and vector stores: org-scoped, visibility-aware, space-aware.

Richer than the CRM tranche because three things can make a resource visible,
and each must widen only in its intended direction:

  1. it belongs to your org (and is shared, or you made it)
  2. it was granted to you by name, inside that org
  3. it was put into a SPACE you belong to, by whoever owns it

Arm 3 is the training use case: one lesson authored once, reachable by people
in many different orgs. It is the only arm that crosses a tenant boundary, and
it is safe because the row can only be written by somebody who OWNS the
resource — so "Acme shares Acme's agent" is expressible and "Acme shares
Beta's agent" is not.

ARM 3 HAS TWO HALVES and both are tested here, because having only the first
is a specific silent bug: the resource appears in the list and 404s when
opened. That is what the catalog did before spaces existed.

    org_scope.shared_resource_ids   what appears in a LIST
    org_scope.shares_resource       whether ONE row opens
"""
import pytest
from sqlalchemy.orm import Session

from src.db.space_models import JOIN_INVITE, SpaceResource
from src.services import spaces
from src.db.models import (
    Agent,
    Organization,
)
from tests.org_isolation import client_for, make_tenant

AGENTS_URL = "/api/agents/"


def _agent(t, name, visibility="private"):
    return Agent(org_id=t.org_id, account_id=t.account_id, name=name,
                 visibility=visibility, config={"data": {}})


async def _visible_ids(tenant):
    async with client_for(tenant) as c:
        resp = await c.get(AGENTS_URL)
    assert resp.status_code == 200, resp.text
    return {a["id"] for a in resp.json()}


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="acme-res", account_id=5301, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="beta-res", account_id=5302, data_scope="shared")


# ---------------------------------------------------------------------------
# the org boundary
# ---------------------------------------------------------------------------

async def test_agents_do_not_cross_orgs(db: Session, _override_db, acme, beta):
    mine = _agent(acme, "Acme Agent", visibility="org")
    db.add(mine); db.flush()
    assert mine.id not in await _visible_ids(beta)


# ---------------------------------------------------------------------------
# visibility, inside one org
# ---------------------------------------------------------------------------

async def test_private_agent_is_hidden_from_colleagues(db: Session, _override_db, acme):
    """Unlike a contact, a resource is not shared with the team by default.

    Someone's half-built agent should not appear for the whole company just
    because they belong to the same org.
    """
    colleague = make_tenant(db, slug="acme-res", account_id=5303, data_scope="shared")
    private = _agent(acme, "Work In Progress", visibility="private")
    db.add(private); db.flush()

    assert private.id in await _visible_ids(acme), "creator must see their own"
    assert private.id not in await _visible_ids(colleague)


async def test_org_visible_agent_is_shared_with_colleagues(db: Session, _override_db, acme):
    colleague = make_tenant(db, slug="acme-res", account_id=5304, data_scope="shared")
    shared = _agent(acme, "Team Agent", visibility="org")
    db.add(shared); db.flush()

    assert shared.id in await _visible_ids(colleague)


async def test_org_visibility_reaches_colleagues_and_stops_at_the_org(
    db: Session, _override_db
):
    """visibility='org' means THIS org, and it is checked at the boundary.

    This test used to assert that visibility='org' did nothing at all inside
    the root org — the guard that stopped one person marking an agent shared
    from exposing it to every signup on the platform. That hazard is gone with
    the lobby: an org now only ever contains people who belong together.

    What still has to hold, and is the easier thing to get wrong, is that
    'org' does not mean 'everyone'. So: a colleague sees it, an outsider never
    does.
    """
    mine = make_tenant(db, slug="vis-team", account_id=5305)
    colleague = make_tenant(db, slug="vis-team", account_id=5306)
    outsider = make_tenant(db, slug="vis-outsider", account_id=5313)
    assert mine.org_id == colleague.org_id != outsider.org_id

    shared = _agent(mine, "Marked Shared", visibility="org")
    private = _agent(mine, "Still Mine", visibility="private")
    db.add_all([shared, private]); db.flush()

    seen_by_colleague = await _visible_ids(colleague)
    assert shared.id in seen_by_colleague, "'org' must reach the team"
    assert private.id not in seen_by_colleague, "private stays private"

    assert shared.id not in await _visible_ids(outsider), \
        "'org' means this org, never another tenant"


# ---------------------------------------------------------------------------
# spaces: sharing a resource across orgs
# ---------------------------------------------------------------------------
#
# This replaced the catalog. Same one-directional property, argued differently:
# the catalog was safe because an item could only reference a PLATFORM-owned
# resource; a space share is safe because the row can only be written by
# somebody who owns the resource. The first needed a privileged org, the second
# does not.


def _shared_space(db, owner_tenant, *, name="Shared"):
    space = spaces.create_space(
        db, name=name, description=None,
        owner_account_id=owner_tenant.account_id,
        owner_org_id=owner_tenant.org_id,
        discoverable=False, join_policy=JOIN_INVITE)
    db.flush()
    return space


def _share(db, space, resource_type, resource_id, by):
    db.add(SpaceResource(space_id=space.id, resource_type=resource_type,
                         resource_id=resource_id, added_by_account_id=by))
    db.flush()


async def test_a_shared_agent_reaches_the_spaces_members(
    db: Session, _override_db, acme, beta
):
    """The cross-org arm, doing the job the catalog used to do."""
    lesson = _agent(acme, "Shared Lesson", visibility="private")
    db.add(lesson); db.flush()

    space = _shared_space(db, acme, name="Training")
    _share(db, space, "agent", lesson.id, acme.account_id)

    assert lesson.id not in await _visible_ids(beta), (
        "not a member yet — the share is not a broadcast")

    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    assert lesson.id in await _visible_ids(beta), (
        "a member of the space reaches what it shares, from another org")


async def test_unsharing_closes_it_on_the_next_request(
    db: Session, _override_db, acme, beta
):
    lesson = _agent(acme, "Temporary", visibility="private")
    db.add(lesson); db.flush()
    space = _shared_space(db, acme, name="Temporary Space")
    _share(db, space, "agent", lesson.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()
    assert lesson.id in await _visible_ids(beta)

    db.query(SpaceResource).filter(SpaceResource.space_id == space.id).delete()
    db.flush()
    assert lesson.id not in await _visible_ids(beta), "revocation is immediate"


async def test_a_space_share_does_not_carry_the_rest_of_the_org(
    db: Session, _override_db, acme, beta
):
    """Sharing ONE agent must not surface the sharer's other rows.

    The arm is an id list, not an org widening — this is the assertion that
    would fail if somebody ever swapped it for "members of a space see the
    owner's org".
    """
    shared = _agent(acme, "Shared", visibility="private")
    private = _agent(acme, "Not Shared", visibility="private")
    db.add_all([shared, private]); db.flush()

    space = _shared_space(db, acme, name="Narrow")
    _share(db, space, "agent", shared.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    visible = await _visible_ids(beta)
    assert shared.id in visible
    assert private.id not in visible, "one share is one row, not the org"


async def test_only_the_owner_of_a_resource_may_share_it(
    db: Session, _override_db, acme, beta
):
    """THE check the cross-org arm rests on.

    If anybody could add any resource_id, joining a space would be a way to
    read arbitrary tenants' agents. Asserted over HTTP, because the rule lives
    in the router.
    """
    victim = _agent(acme, "Acme Private", visibility="private")
    db.add(victim); db.flush()

    # Beta owns a space and tries to share ACME's agent into it.
    space = _shared_space(db, beta, name="Beta Space")
    db.commit()

    async with client_for(beta) as c:
        resp = await c.post(f"/api/spaces/{space.id}/resources", json={
            "resource_type": "agent", "resource_id": victim.id})

    assert resp.status_code == 404
    assert db.query(SpaceResource).filter(
        SpaceResource.space_id == space.id).count() == 0


async def test_crm_rows_are_not_shareable(db: Session, _override_db, acme):
    """The whitelist, asserted rather than trusted.

    Tenant data must never become shareable by somebody passing a new string.
    """
    space = _shared_space(db, acme, name="No CRM")
    db.commit()

    async with client_for(acme) as c:
        resp = await c.post(f"/api/spaces/{space.id}/resources", json={
            "resource_type": "contact", "resource_id": 1})
    assert resp.status_code == 422


async def test_a_space_member_can_actually_OPEN_the_shared_agent(
    db: Session, _override_db, acme, beta
):
    """The half a list predicate cannot cover.

    Listing and opening are different queries, and for most of this platform's
    life only the first knew about cross-org sharing. A catalog-published agent
    showed up in the client's list and then 404'd on click, because GET-one
    resolves through can_access_agent rather than through the list predicate.
    If this test fails, that bug is back.
    """
    lesson = _agent(acme, "Openable Lesson", visibility="private")
    db.add(lesson); db.flush()

    space = _shared_space(db, acme, name="Openable")
    _share(db, space, "agent", lesson.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.commit()

    async with client_for(beta) as c:
        resp = await c.get(f"{AGENTS_URL}{lesson.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(lesson.id) or resp.json()["id"] == lesson.id


async def test_opening_is_still_refused_for_an_unshared_agent(
    db: Session, _override_db, acme, beta
):
    """The same query, from the same person, one row over.

    Pinned separately from the list test because the open path is where a
    too-wide fix would land: 'members of a space may open the owner's agents'
    passes every list assertion in this file and fails only here.
    """
    shared = _agent(acme, "Shared", visibility="private")
    private = _agent(acme, "Private", visibility="private")
    db.add_all([shared, private]); db.flush()

    space = _shared_space(db, acme, name="One Row")
    _share(db, space, "agent", shared.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.commit()

    async with client_for(beta) as c:
        assert (await c.get(f"{AGENTS_URL}{shared.id}")).status_code == 200
        assert (await c.get(f"{AGENTS_URL}{private.id}")).status_code == 404


def test_a_space_share_is_read_only(db: Session, acme, beta):
    """A space says "use this", never "reconfigure this".

    Write still resolves through services/access.py, which is org-confined with
    no exceptions — so the space arm can widen what is readable and can never
    widen what is writable. Asserted at the service level because that is where
    the asymmetry lives.
    """
    from src.db.models import VectorStore
    from src.services.org_scope import shares_resource, VECTOR_STORE
    from src.services import access

    store = VectorStore(org_id=acme.org_id, owner_account_id=acme.account_id,
                        index_name="shared-kb", visibility="private")
    db.add(store); db.flush()

    space = _shared_space(db, acme, name="Read Only")
    _share(db, space, "vector_store", store.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    assert shares_resource(db, beta.account_id, VECTOR_STORE, store.id), (
        "the space arm reaches it for reading")
    assert not access.can_access(db, beta.account_id, access.VECTOR_STORE,
                                 store.id, required="write",
                                 org_id=beta.org_id), (
        "and never for writing — that would need a grant, which is org-confined")


def test_the_audit_answers_with_the_space_that_carries_it(db: Session, acme, beta):
    """"Who can reach this?" must count the arm that leaves the org.

    An access audit that undercounts is worse than none, and the space arm is
    exactly the one somebody would forget to join in — it lives in a different
    table from every other answer on the page.
    """
    from src.services import access

    lesson = _agent(acme, "Audited", visibility="private")
    db.add(lesson); db.flush()

    space = _shared_space(db, acme, name="Audited Space")
    _share(db, space, "agent", lesson.id, acme.account_id)
    spaces.add_member(db, space=space, account_id=beta.account_id,
                      tier_key="member", actor_account_id=acme.account_id)
    db.flush()

    reached = access.effective_accounts(db, access.AGENT, lesson.id)
    by_account = {r["account_id"]: r for r in reached}

    assert by_account[acme.account_id]["via"] == "owner"
    assert by_account[beta.account_id]["via"] == "space:Audited Space", (
        "the audit names the space, so a reader knows where to go to revoke it")

    # And the reverse question, from the other end.
    theirs = access.resources_for_account(db, beta.account_id)
    assert {"resource_type": "agent", "resource_id": lesson.id,
            "role": "read", "via": "space:Audited Space"} in theirs
