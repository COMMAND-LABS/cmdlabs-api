"""
Contract test for the canonical agent access-control rule.

`src/services/agent_access.py` is byte-identical across ai-api and agent-api
(enforced by repo-root check-schemas.sh). This test is the *behavioral* half of
that guarantee: it proves the rule actually behaves the same. The agent-api copy
(tests/test_agent_access_contract.py there) exercises the identical scenarios —
keep the two in sync when adding cases.

THREE ARMS, AND THE THIRD IS THE ONE THAT CROSSES AN ORG
-------------------------------------------------------
    own it                                    → yes
    a grant naming you, in this org           → yes
    a space you are in, that it was put into  → yes

The third arm replaced access groups. A group was a set of accounts inside one
org that a grant could name; a space is a set of accounts that may come from
several. The tests below pin down what that changed and — more importantly —
what it did not: being in a space with somebody still reaches nothing except
what was deliberately put in the space.
"""
import pytest

from src.db.models import (
    Account,
    Agent,
    AccessGrant,
)
from src.db.space_models import Space, SpaceMember, SpaceResource
from src.services.agent_access import (
    can_access_agent,
    get_accessible_agent_ids,
    load_agent_with_access_check,
)

# Every row needs a tenant now that org_id is NOT NULL. These suites are
# single-tenant, so they all sit in the root org conftest creates.
ROOT_ORG_ID = 1

OWNER, GRANTEE, OUTSIDER = 1001, 1002, 1003
SPACE_MEMBER, OTHER_SPACE_MEMBER = 1004, 1005
AGENT_ID, UNSHARED_AGENT_ID = 2001, 2002
SHARED_SPACE, OTHER_SPACE = 3001, 3002
MISSING_AGENT_ID = 999999


@pytest.fixture()
def seed(db):
    """One agent reachable three ways, and one reachable only by its owner."""
    for acc_id, email in [
        (OWNER, "owner@example.com"),
        (GRANTEE, "grantee@example.com"),
        (OUTSIDER, "outsider@example.com"),
        (SPACE_MEMBER, "in-the-space@example.com"),
        (OTHER_SPACE_MEMBER, "in-another-space@example.com"),
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

    # Arm 3: the agent put into a space, whose members reach it.
    db.add(Space(id=SHARED_SPACE, name="Shared", owner_account_id=OWNER,
                 owner_org_id=ROOT_ORG_ID))
    db.add(Space(id=OTHER_SPACE, name="Unrelated", owner_account_id=OWNER,
                 owner_org_id=ROOT_ORG_ID))
    db.add(SpaceMember(space_id=SHARED_SPACE, account_id=SPACE_MEMBER,
                       tier_key="member"))
    db.add(SpaceMember(space_id=OTHER_SPACE, account_id=OTHER_SPACE_MEMBER,
                       tier_key="member"))
    db.add(SpaceResource(space_id=SHARED_SPACE, resource_type='agent',
                         resource_id=AGENT_ID, added_by_account_id=OWNER))
    db.flush()
    return db


def test_owner_can_access(seed):
    assert can_access_agent(seed, OWNER, AGENT_ID)


def test_a_named_grantee_can_access(seed):
    assert can_access_agent(seed, GRANTEE, AGENT_ID)


def test_a_member_of_the_space_it_was_shared_into_can_access(seed):
    assert can_access_agent(seed, SPACE_MEMBER, AGENT_ID)


def test_a_member_of_a_different_space_cannot(seed):
    """Being in SOME space reaches nothing. The share names one space."""
    assert not can_access_agent(seed, OTHER_SPACE_MEMBER, AGENT_ID)


def test_the_space_reaches_only_what_was_put_in_it(seed):
    """The sharpest edge in the whole design.

    SPACE_MEMBER reaches AGENT_ID. Its owner also owns UNSHARED_AGENT_ID, in
    the same org, created by the same account. If space membership ever leaked
    into "you may see this person's agents", this is the assertion that fails.
    """
    assert not can_access_agent(seed, SPACE_MEMBER, UNSHARED_AGENT_ID)


def test_outsider_cannot_access(seed):
    assert not can_access_agent(seed, OUTSIDER, AGENT_ID)


def test_missing_agent_is_denied(seed):
    assert not can_access_agent(seed, OWNER, MISSING_AGENT_ID)


def test_get_accessible_agent_ids_is_grants_only(seed):
    """Deliberately narrower than can_access_agent, and it must stay that way.

    This function feeds the LIST queries, which add the space arm themselves
    through org_scope.scoped_resources — beside the org predicate the arm has
    to sit next to. Returning space ids here as well would apply that arm twice
    at two different widths.
    """
    assert get_accessible_agent_ids(seed, GRANTEE) == {AGENT_ID}
    assert get_accessible_agent_ids(seed, SPACE_MEMBER) == set()
    assert get_accessible_agent_ids(seed, OWNER) == set()
    assert get_accessible_agent_ids(seed, OUTSIDER) == set()


def test_load_agent_with_access_check(seed):
    assert load_agent_with_access_check(seed, OWNER, AGENT_ID).id == AGENT_ID
    assert load_agent_with_access_check(seed, GRANTEE, AGENT_ID).id == AGENT_ID
    assert load_agent_with_access_check(seed, SPACE_MEMBER, AGENT_ID).id == AGENT_ID
    assert load_agent_with_access_check(seed, OUTSIDER, AGENT_ID) is None
    assert load_agent_with_access_check(seed, OWNER, MISSING_AGENT_ID) is None
