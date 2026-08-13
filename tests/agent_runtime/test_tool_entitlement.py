"""
The agent runtime honours module entitlement.

cmdlabs-api gates its HTTP surface with require_module(), so a member whose tier
excludes Contacts gets a 404 from /api/contacts. That closes the front door
only. An agent's tools read the same tables from THIS service, over their own
sessions, and knew nothing about modules — so the same member could ask an agent
to list their contacts and simply get them. The entitlement was a locked door
next to an open window.

Two layers, matching test_contact_crm_tools.py:
  - pure resolution of a tool list against a module set (no DB), and
  - effective_modules against a real org/tier/member (Postgres, skipped cleanly
    when unreachable).

Note what is NOT asserted here: that the tool refuses at call time. A tool the
caller may not use is never BUILT, so it is absent from the model's tool list
entirely. Absent beats refusing — the model cannot narrate, or be talked into
retrying, a capability that was never offered.
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.config import plans_registry as plans
from src.config.roles_registry import (
    COMMUNITY_MODULES, ROLE_COMMUNITY_MEMBER, ROLE_MANAGER,
)
from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
)
from src.agent_runtime.tool_entitlement import (
    TOOL_MODULES,
    allowed_tool_configs,
    effective_modules,
)

# --------------------------------------------------------------------------
# Tool -> module resolution (no DB)
# --------------------------------------------------------------------------

CRM_AGENT = [
    {"type": "contactRead"},
    {"type": "contactEventsRead"},
    {"type": "vectorSearch", "index": "kb"},
    {"type": "dbTableRead"},
]


def test_ungranted_tools_are_dropped():
    kept = {c["type"] for c in allowed_tool_configs(CRM_AGENT, {"knowledge_bases"})}
    assert "vectorSearch" in kept
    assert "contactRead" not in kept, (
        "a caller whose tier excludes Contacts must not get CRM tools")


def test_ungated_tools_survive_an_empty_grant():
    """Raw DB read/write is bound to a credential the caller already had to
    hold, not to a product module — gating it on one would be arbitrary."""
    kept = {c["type"] for c in allowed_tool_configs(CRM_AGENT, set())}
    assert kept == {"dbTableRead"}


def test_every_registered_tool_type_is_classified():
    """An unlisted tool type defaults to ungated, so the map must name them all
    — an oversight should be visible here rather than shipping unguarded."""
    from src.agent_runtime.tools import ToolRegistry

    unclassified = set(ToolRegistry.list_types()) - set(TOOL_MODULES)
    assert not unclassified, (
        f"These tool types are not classified in TOOL_MODULES: {sorted(unclassified)}\n"
        "Map each to a module key, or to None with a reason."
    )


# --------------------------------------------------------------------------
# effective_modules against a real org (Postgres)
# --------------------------------------------------------------------------

_PG_URL = os.environ.get(
    "POSTGRES_TEST_URL", "postgresql://test:test@cmdlabs-test-pg:5432/kalygo_test"
)

try:
    _engine = create_engine(_PG_URL, connect_args={"connect_timeout": 3})
    with _engine.connect():
        pass
    _conn_ok = True
except Exception:  # noqa: BLE001
    _conn_ok = False

pg_required = pytest.mark.skipif(
    not _conn_ok, reason=f"Postgres test DB not reachable at {_PG_URL}"
)


@pytest.fixture()
def pg():
    connection = _engine.connect()
    trans = connection.begin()
    Session = sessionmaker(bind=connection)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()


def _org(session, slug, plan, role, account_id, is_owner=False):
    # `slug` is the caller's label for the org in this test, not a column:
    # organizations dropped their slug in f4a5b6c7d8f0. Kept as the parameter
    # name because it is what every call site reads as "which org is this".
    #
    # `plan` is PINNED, so the ceiling does not depend on an owner account with
    # a subscription these tests do not care about.
    # `is_owner` is now the ORG's owner_account_id rather than a flag on the
    # membership row — one fact, one home, matching cmdlabs-api. The parameter
    # and every call site are unchanged; only where it gets written moved.
    org = Organization(name=slug, pinned_plan=plan)
    session.add(org)
    session.flush()
    session.add(Account(id=account_id, email=f"{slug}-{account_id}@t.test",
                        default_org_id=org.id))
    session.flush()
    # After the account exists: owner_account_id is a foreign key, so naming an
    # owner before there is one to name is a constraint violation rather than a
    # silent inconsistency — which is rather the point of having one column.
    if is_owner:
        org.owner_account_id = account_id
        session.flush()
    session.add(OrganizationMember(org_id=org.id, account_id=account_id,
                                   role=role, granted_by="grant"))
    session.flush()
    return org


@pg_required
def test_effective_modules_is_ceiling_intersect_role(pg):
    """The narrow role on a premium org: the role is what withholds.

    Asserted against COMMUNITY_MODULES rather than a literal set, so this
    tracks the registry — and it must equal it exactly, because every key in
    that allowlist is inside the premium ceiling.
    """
    org = _org(pg, "ent-a", plan="premium", role=ROLE_COMMUNITY_MEMBER,
               account_id=77001)
    assert effective_modules(pg, 77001, org.id) == set(COMMUNITY_MODULES)


@pg_required
def test_an_owner_gets_the_whole_ceiling(pg):
    # The SMALLEST role, which is what the roles migration left every row on.
    org = _org(pg, "ent-b", plan="free", role=ROLE_COMMUNITY_MEMBER,
               account_id=77002, is_owner=True)
    assert effective_modules(pg, 77002, org.id) == set(
        plans.modules_for_plan(plans.PLAN_FREE))


@pg_required
def test_a_non_member_gets_nothing(pg):
    """Fails closed. This check runs outside the request context, so it cannot
    lean on get_org_context having already refused."""
    org = _org(pg, "ent-c", plan="premium", role=ROLE_MANAGER,
               account_id=77003)
    pg.add(Account(id=77004, email="stranger@t.test"))
    pg.flush()
    assert effective_modules(pg, 77004, org.id) == set()


@pg_required
def test_a_role_without_contacts_gets_no_crm_tools(pg):
    """The end-to-end shape: entitlement resolved from the database, then
    applied to a real agent's tool list.

    THIS IS THE FILE'S REASON TO EXIST. The HTTP surface refusing /api/contacts
    closes the front door; without this, the same person could ask an agent to
    list their contacts and get them. A community member has no `contacts`
    module, so the tool is never built.
    """
    org = _org(pg, "ent-d", plan="premium", role=ROLE_COMMUNITY_MEMBER,
               account_id=77005)
    granted = effective_modules(pg, 77005, org.id)
    kept = {c["type"] for c in allowed_tool_configs(CRM_AGENT, granted)}
    assert "contactRead" not in kept, (
        "a community member must not reach the CRM through an agent either")
