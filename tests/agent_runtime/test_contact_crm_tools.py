"""Tests for the structurally-scoped contact_crm tools.

Two layers:
  - Structural (no DB): the security guarantee — no contact_id / account_id
    parameter on any tool. Always runs.
  - Behavior (Postgres test DB, skipped if unavailable): scoping and account
    isolation against a real database via an injected session factory.
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Account, Contact, ContactEvent, Organization
from src.services.org_scope import OrgScope
from src.agent_runtime.tools.contact_crm import (
    create_contact_event_write_tool,
    create_contact_events_read_tool,
    create_contact_read_tool,
)


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Structural guarantee (no DB) — the core of the whole design.
# --------------------------------------------------------------------------

def test_no_tool_exposes_contact_or_account_id():
    builders = [
        create_contact_read_tool,
        create_contact_events_read_tool,
        create_contact_event_write_tool,
    ]
    for builder in builders:
        tool = _run(builder({}, account_id=1, org_scope=_scope(1), contact_id=42))
        fields = set(tool.args_schema.model_fields)
        assert "contact_id" not in fields, f"{tool.name} leaks contact_id"
        assert "account_id" not in fields, f"{tool.name} leaks account_id"


def test_tool_names_are_stable():
    assert _run(create_contact_read_tool({}, 1, org_scope=_scope(1), contact_id=1)).name == "get_contact"
    assert _run(create_contact_events_read_tool({}, 1, org_scope=_scope(1), contact_id=1)).name == "list_contact_events"
    assert _run(create_contact_event_write_tool({}, 1, org_scope=_scope(1), contact_id=1)).name == "add_contact_event"


def test_tools_fail_safe_when_no_contact_bound():
    # Defensive: prepare_agent_context fails closed before this, but the tool
    # must never run unscoped if contact_id is somehow None.
    tool = _run(create_contact_read_tool({}, account_id=1, org_scope=_scope(1), contact_id=None))
    result = _run(tool.coroutine())
    assert "error" in result


# --------------------------------------------------------------------------
# Behavior (Postgres) — skipped cleanly if the test DB is not reachable.
# --------------------------------------------------------------------------

_PG_URL = os.environ.get(
    "POSTGRES_TEST_URL", "postgresql://test:test@cmdlabs-test-pg:5432/kalygo_test"
)

try:
    _engine = create_engine(_PG_URL, connect_args={"connect_timeout": 3})
    _conn_ok = True
    with _engine.connect():
        pass
except Exception:  # noqa: BLE001
    _conn_ok = False

pg_required = pytest.mark.skipif(
    not _conn_ok, reason=f"Postgres test DB not reachable at {_PG_URL}"
)


@pytest.fixture()
def pg():
    """Transactional session factory bound to one connection (rolled back)."""
    connection = _engine.connect()
    trans = connection.begin()
    Session = sessionmaker(bind=connection)
    seed = Session()
    try:
        yield Session, seed
    finally:
        seed.close()
        trans.rollback()
        connection.close()



# Tools are built with an explicit tenant scope because they run on their own
# session, after the request session has closed. ORG=1 is the root org the
# seed data below lives in.
ORG_ID = 1


def _scope(account_id: int, **_ignored) -> OrgScope:
    """Orgs no longer carry a data_scope; kwargs are tolerated so the
    existing call sites read unchanged."""
    return OrgScope(account_id=account_id, org_id=ORG_ID)


def _ensure_org(session):
    """org_id is NOT NULL on contacts, so the tenant has to exist first."""
    org = session.query(Organization).filter(Organization.id == ORG_ID).first()
    if org is None:
        org = Organization(id=ORG_ID, name="CMD LABS")
        session.add(org)
        session.flush()
    return org


def _seed_contact(session, account_id, email):
    _ensure_org(session)
    if not session.query(Account).filter(Account.id == account_id).first():
        session.add(Account(id=account_id, email=f"acct{account_id}-{email}"))
        session.flush()
    c = Contact(org_id=ORG_ID, account_id=account_id, first_name="Rodolfo", last_name="C", email=email)
    session.add(c)
    session.flush()
    return c


@pg_required
def test_get_contact_returns_only_bound_contact(pg):
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")

    tool = _run(create_contact_read_tool({}, account_id=1, org_scope=_scope(1), contact_id=c.id,
                                         session_factory=Session))
    result = _run(tool.coroutine())
    assert result["id"] == c.id
    assert result["email"] == c.email


@pg_required
def test_a_colleague_in_the_same_org_can_read_the_contact(pg):
    """Same org, different account: found. That is what a shared org means.

    This asserted the opposite until org-per-signup, because the root org held
    every unrelated signup and account_id was doing the isolating. Now two
    accounts are only ever in one org together if they are colleagues, so
    hiding a teammate's contact would be the bug.

    Kept as a positive assertion rather than deleted: it is what fails if
    somebody reintroduces an account_id filter alongside the tenant predicate,
    which would look like a harmless belt-and-braces tightening and would
    quietly break every team.
    """
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")

    tool = _run(create_contact_read_tool({}, account_id=999, org_scope=_scope(999),
                                         contact_id=c.id, session_factory=Session))
    result = _run(tool.coroutine())
    assert result.get("id") == c.id
    assert result.get("account_id", 1) == 1, "attribution still names the creator"


@pg_required
def test_add_event_forces_scope_and_is_listed(pg):
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")

    write = _run(create_contact_event_write_tool({}, account_id=1, org_scope=_scope(1), contact_id=c.id,
                                                 session_factory=Session))
    out = _run(write.coroutine(event_type="call", title="Intro call",
                               description="Discussed pricing"))
    assert out["success"] is True
    assert out["event"]["event_type"] == "call"

    # The written row carries the forced scope.
    row = seed.query(ContactEvent).filter(ContactEvent.id == out["event"]["id"]).first()
    assert row.contact_id == c.id
    assert row.account_id == 1

    read = _run(create_contact_events_read_tool({}, account_id=1, org_scope=_scope(1), contact_id=c.id,
                                                session_factory=Session))
    listed = _run(read.coroutine())
    assert any(e["title"] == "Intro call" for e in listed["events"])


@pg_required
def test_list_events_is_visible_to_a_colleague(pg):
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")
    seed.add(ContactEvent(org_id=ORG_ID, contact_id=c.id, account_id=1,
                          event_type="note", title="private"))
    seed.flush()

    read = _run(create_contact_events_read_tool({}, account_id=999, org_scope=_scope(999),
                                                contact_id=c.id, session_factory=Session))
    listed = _run(read.coroutine())
    assert [e["title"] for e in listed["events"]] == ["private"], (
        "a colleague in the same org sees the org's events; the boundary is "
        "the org, and test_list_events_does_not_cross_orgs covers that one")


# ---------------------------------------------------------------------------
# cross-ORG isolation
# ---------------------------------------------------------------------------
# The cases above vary account_id within one org. These vary the ORG, which is
# the boundary that actually matters — and which the agent runtime had no way
# to enforce until the tool scope was threaded through, because tools run on
# their own session with no ambient request context.

def _ensure_second_org(session, org_id=770, name="Beta"):
    org = session.query(Organization).filter(Organization.id == org_id).first()
    if org is None:
        org = Organization(id=org_id, name=name)
        session.add(org); session.flush()
    return org


@pg_required
def test_get_contact_does_not_cross_orgs(pg):
    """Same account id, different org: the contact must not be found.

    Without the scope the tool filtered on account_id alone, so an agent run
    in one tenant could read a contact in another as long as the ids lined up.
    """
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")   # lives in ORG_ID
    other = _ensure_second_org(seed)

    tool = _run(create_contact_read_tool(
        {}, account_id=1,
        org_scope=OrgScope(account_id=1, org_id=other.id),
        contact_id=c.id, session_factory=Session))
    assert _run(tool.coroutine()) == {"error": "Contact not found."}


@pg_required
def test_list_events_does_not_cross_orgs(pg):
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")
    seed.add(ContactEvent(org_id=ORG_ID, contact_id=c.id, account_id=1,
                          event_type="note", title="private"))
    seed.flush()
    other = _ensure_second_org(seed)

    tool = _run(create_contact_events_read_tool(
        {}, account_id=1,
        org_scope=OrgScope(account_id=1, org_id=other.id),
        contact_id=c.id, session_factory=Session))
    result = _run(tool.coroutine())
    events = result.get("events", result) if isinstance(result, dict) else result
    assert not events, f"cross-org event leak: {events}"


@pg_required
def test_written_event_is_stamped_with_the_running_org(pg):
    """The write path must record the tenant, not only the author — otherwise
    the row is unreachable by the org scoping that will read it back."""
    Session, seed = pg
    c = _seed_contact(seed, 1, f"{uuid.uuid4()}@x.com")

    write = _run(create_contact_event_write_tool(
        {}, account_id=1, org_scope=_scope(1), contact_id=c.id,
        session_factory=Session))
    _run(write.coroutine(event_type="note", title="stamped"))

    ev = (seed.query(ContactEvent)
          .filter(ContactEvent.contact_id == c.id,
                  ContactEvent.title == "stamped")
          .one())
    assert ev.org_id == ORG_ID
