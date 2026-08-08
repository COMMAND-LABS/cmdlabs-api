"""
Agents and vector stores: org-scoped and visibility-aware.

Two things can make a resource visible, and each must widen only in its
intended direction:

  1. it belongs to your org (and is shared, or you made it)
  2. it was granted to you by name, inside that org

NEITHER CROSSES A TENANT BOUNDARY, and that is the property this file exists to
pin down. There was a third arm — the resource was put into a SPACE you belong
to, by whoever owns it — which was the training use case: one lesson authored
once, reachable by people in many orgs. Spaces were removed, and roughly 240
lines of this file went with them.

Whatever restores cross-org reach needs those tests back, including the pair
that checked BOTH HALVES of the arm. Having only the first is a specific silent
bug: the resource appears in the list and 404s when opened. That is what the
catalog did before spaces existed, and it is what a list predicate without its
single-row twin will do again.
"""
import pytest
from sqlalchemy.orm import Session

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
