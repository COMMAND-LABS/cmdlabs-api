"""
Contract test for the canonical agent access-control rule.

`src/services/agent_access.py` is byte-identical across ai-api and agent-api
(enforced by repo-root check-schemas.sh). This test is the *behavioral* half of
that guarantee: it proves the rule actually behaves the same. The agent-api copy
(tests/test_agent_access_contract.py there) exercises the identical scenarios —
keep the two in sync when adding cases.

TWO ARMS, NEITHER OF WHICH CROSSES AN ORG
-----------------------------------------
    own it                                    → yes
    a grant naming you, in this org           → yes

There was a third — "a space you are in, that it was put into" — and it was the
only one that left the tenant. It replaced access groups: a group was a set of
accounts inside one org that a grant could name; a space was a set of accounts
that could come from several.

WHAT THOSE TESTS PINNED DOWN, for whoever restores the arm. Not that a space
member could reach the shared agent — that is the easy half — but that being in
a space with somebody reached NOTHING ELSE:

    a member of a DIFFERENT space reached it not at all;
    a member of the sharing space could not reach the owner's OTHER agents,
      same org, same creator, simply not put in the space;
    get_accessible_agent_ids stayed grants-only, because the LIST queries add
      the sharing arm themselves and returning it here too applies it twice at
      two different widths.

That third assertion is the sharpest edge in the design and the easiest to lose.
"""
import pytest

from src.db.models import (
    Account,
    Agent,
    AccessGrant,
)
from src.services.agent_access import (
    can_access_agent,
    get_accessible_agent_ids,
    load_agent_with_access_check,
)

# Every row needs a tenant now that org_id is NOT NULL. These suites are
# single-tenant, so they all sit in the root org conftest creates.
ROOT_ORG_ID = 1

OWNER, GRANTEE, OUTSIDER = 1001, 1002, 1003
AGENT_ID, UNSHARED_AGENT_ID = 2001, 2002
MISSING_AGENT_ID = 999999


@pytest.fixture()
def seed(db):
    """One agent reachable two ways, and one reachable only by its owner."""
    for acc_id, email in [
        (OWNER, "owner@example.com"),
        (GRANTEE, "grantee@example.com"),
        (OUTSIDER, "outsider@example.com"),
    ]:
        db.add(Account(id=acc_id, email=email))
    db.add(Agent(org_id=ROOT_ORG_ID, id=AGENT_ID, account_id=OWNER,
                 name="SOP Agent", config={"data": {}}))
    db.add(Agent(org_id=ROOT_ORG_ID, id=UNSHARED_AGENT_ID, account_id=OWNER,
                 name="Private Agent", config={"data": {}}))

    # Arm 2: a grant naming ONE person.
    db.add(AccessGrant(
        org_id=ROOT_ORG_ID,
        principal_type='account',
        principal_id=GRANTEE,
        resource_type='agent',
        resource_id=AGENT_ID,
        role='use',
    ))
    db.flush()
    return db


def test_owner_can_access(seed):
    assert can_access_agent(seed, OWNER, AGENT_ID)


def test_a_named_grantee_can_access(seed):
    assert can_access_agent(seed, GRANTEE, AGENT_ID)


def test_a_grant_reaches_only_the_agent_it_names(seed):
    """A grant on AGENT_ID says nothing about the owner's other agents.

    Same org, same creator, no grant — GRANTEE must not reach it. This is the
    surviving half of the assertion the space tests made: reaching one of
    somebody's agents never means reaching the rest of them.
    """
    assert not can_access_agent(seed, GRANTEE, UNSHARED_AGENT_ID)


def test_outsider_cannot_access(seed):
    assert not can_access_agent(seed, OUTSIDER, AGENT_ID)


def test_missing_agent_is_denied(seed):
    assert not can_access_agent(seed, OWNER, MISSING_AGENT_ID)


def test_get_accessible_agent_ids_is_grants_only(seed):
    """Deliberately narrower than can_access_agent, and it must stay that way.

    It EXCLUDES owned agents — callers union those separately — which is why
    OWNER comes back empty here despite owning both. It also excluded the space
    arm, because the LIST queries add that themselves through
    org_scope.scoped_resources, beside the org predicate it has to sit next to.
    Returning shared ids here as well would apply that arm twice at two
    different widths; that reasoning applies again to whatever replaces spaces.
    """
    assert get_accessible_agent_ids(seed, GRANTEE) == {AGENT_ID}
    assert get_accessible_agent_ids(seed, OWNER) == set()
    assert get_accessible_agent_ids(seed, OUTSIDER) == set()


def test_load_agent_with_access_check(seed):
    assert load_agent_with_access_check(seed, OWNER, AGENT_ID).id == AGENT_ID
    assert load_agent_with_access_check(seed, GRANTEE, AGENT_ID).id == AGENT_ID
    assert load_agent_with_access_check(seed, OUTSIDER, AGENT_ID) is None
    assert load_agent_with_access_check(seed, OWNER, MISSING_AGENT_ID) is None
