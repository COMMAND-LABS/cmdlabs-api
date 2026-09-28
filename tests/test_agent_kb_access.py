"""Reading an agent's knowledge base takes a read grant on it.

Using an agent does not by itself let someone read the knowledge bases its
vector-search tools search (services/agent_knowledge_bases.py). Without a read
grant the search tool is not built for them, its source files do not open, the
chat is told which knowledge bases they lack, and the owner's audit names them.
Sharing the agent can grant the knowledge bases in the same step, and the
backfill migration does that for every share made before the rule.
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from src.agent_runtime.tools import vector_search as vector_search_mod
from src.agent_runtime.tools.vector_search import create_vector_search_tool
from src.config import plans_registry as plans
from src.config.roles_registry import ROLE_COMMUNITY_MEMBER
from src.db.models import (
    AccessGrant, AccessGrantEvent, Agent, Organization, VectorDbIngestionLog, VectorStore,
)
from src.services import access, account_gcs_service
from tests.org_isolation import client_for, make_tenant

INDEX, NAMESPACE = "kb-index", "pedestal"
TOOL = {"type": "vectorSearch", "provider": "pinecone",
        "index": INDEX, "namespace": NAMESPACE, "topK": 5}
CONFIG = {"schema": "agent_config", "version": 4,
          "data": {"systemPrompt": "hi", "tools": [TOOL]}}


def _source(namespace: str = NAMESPACE, index: str = INDEX) -> str:
    return f"vector_stores/{index}/{namespace}/batch-1/file-1/note.md"


@pytest.fixture()
def owner(db: Session):
    t = make_tenant(db, slug="kb-access", account_id=9301)
    org = db.query(Organization).filter(Organization.id == t.org_id).one()
    org.pinned_plan = plans.PLAN_PREMIUM
    db.flush()
    return t


@pytest.fixture()
def member(db: Session, owner):
    return make_tenant(db, slug="kb-access", account_id=9302,
                       role=ROLE_COMMUNITY_MEMBER, is_owner=False)


@pytest.fixture()
def store(db: Session, owner):
    vs = VectorStore(owner_account_id=owner.account_id, index_name=INDEX, org_id=owner.org_id)
    db.add(vs)
    db.flush()
    return vs


@pytest.fixture()
def agent(db: Session, owner):
    a = Agent(org_id=owner.org_id, account_id=owner.account_id,
              name="Analyst", visibility="private", config=CONFIG)
    db.add(a)
    db.flush()
    return a


def _grant(db: Session, tenant, resource_type: str, resource_id: int, role: str):
    db.add(AccessGrant(org_id=tenant.org_id, principal_type=access.ACCOUNT,
                       principal_id=tenant.account_id, resource_type=resource_type,
                       resource_id=resource_id, role=role))
    db.flush()


@pytest.fixture()
def shared_agent(db: Session, agent, member):
    """The agent shared with the member — and nothing else."""
    _grant(db, member, access.AGENT, agent.id, "use")
    return agent


@pytest.fixture()
def kb_reader(db: Session, member, store):
    _grant(db, member, access.VECTOR_STORE, store.id, "read")
    return member


# ── The vector-search tool ──────────────────────────────────────────────────

@pytest.fixture()
def fake_pinecone(monkeypatch):
    loads = []

    def load(tool_config, account_id, db, **kw):
        loads.append(account_id)
        return MagicMock(), NAMESPACE, INDEX

    monkeypatch.setattr(vector_search_mod, "load_pinecone_index", load)
    return loads


async def _build(db, tenant, owner):
    return await create_vector_search_tool(
        tool_config=TOOL, account_id=tenant.account_id, db=db,
        agent_owner_account_id=owner.account_id,
        org_scope=MagicMock(org_id=owner.org_id),
    )


async def test_the_owner_gets_search(db: Session, owner, store, fake_pinecone):
    assert await _build(db, owner, owner) is not None


async def test_sharing_the_agent_alone_does_not_give_search(
    db: Session, owner, member, shared_agent, store, fake_pinecone
):
    assert await _build(db, member, owner) is None
    assert fake_pinecone == [], "skipped before touching the owner's credentials"


async def test_a_read_grant_gives_search(
    db: Session, owner, kb_reader, shared_agent, fake_pinecone
):
    assert await _build(db, kb_reader, owner) is not None


# ── Opening a source file ───────────────────────────────────────────────────

@pytest.fixture()
def signed(monkeypatch):
    """Record which signer ran instead of touching GCS."""
    calls = []

    def recorded(db, owner_account_id, **kw):
        calls.append(("recorded", owner_account_id, kw["gcs_bucket"], kw["gcs_file_path"]))
        return "https://signed/recorded"

    def by_index(db, owner_account_id, index_name, **kw):
        calls.append(("index", owner_account_id, index_name, kw["gcs_file_path"]))
        return "https://signed/index"

    monkeypatch.setattr(account_gcs_service, "generate_signed_url_for", recorded)
    monkeypatch.setattr(account_gcs_service, "generate_signed_url_for_index", by_index)
    return calls


async def _open(tenant, agent, path):
    async with client_for(tenant) as c:
        return await c.get("/api/files/source-url",
                           params={"agent_id": agent.id, "path": path})


async def test_a_reader_opens_a_source_with_no_ingestion_log_row(
    _override_db, owner, kb_reader, shared_agent, signed
):
    """Whoever wrote the note: the log row is best-effort, so it is not required."""
    resp = await _open(kb_reader, shared_agent, _source())
    assert resp.status_code == 200, resp.text
    assert resp.json()["url"] == "https://signed/index"
    assert signed == [("index", owner.account_id, INDEX, _source())]


async def test_the_bucket_recorded_at_ingest_wins(
    db: Session, _override_db, owner, kb_reader, shared_agent, signed
):
    db.add(VectorDbIngestionLog(
        account_id=owner.account_id, provider="pinecone", index_name=INDEX,
        namespace=NAMESPACE, operation_type="INGEST", status="PENDING",
        gcs_bucket="recorded-bucket", gcs_file_path=_source(),
    ))
    db.flush()

    resp = await _open(kb_reader, shared_agent, _source())
    assert resp.status_code == 200, resp.text
    assert signed == [("recorded", owner.account_id, "recorded-bucket", _source())]


async def test_agent_users_without_a_read_grant_cannot_open_sources(
    _override_db, member, shared_agent, store, signed
):
    resp = await _open(member, shared_agent, _source())
    assert resp.status_code == 404
    assert signed == []


@pytest.mark.parametrize("path", [
    _source(namespace="other"),                            # a namespace the agent doesn't search
    _source(index="other-index"),                          # an index the agent doesn't search
    f"vector_stores/{INDEX}/{NAMESPACE}/note.md",          # not the upload layout
    f"vector_stores/{INDEX}/{NAMESPACE}/x/y/sub/note.md",  # nested-namespace look-alike
    f"vector_stores/{INDEX}/{NAMESPACE}/../other/b/f/n.md",
    "credentials/service-account.json",
])
async def test_paths_outside_the_agents_namespaces_are_refused(
    _override_db, kb_reader, shared_agent, signed, path
):
    resp = await _open(kb_reader, shared_agent, path)
    assert resp.status_code == 404
    assert signed == []


async def test_an_account_without_the_agent_cannot_open_its_sources(
    db: Session, _override_db, shared_agent, store, signed
):
    stranger = make_tenant(db, slug="kb-access-other", account_id=9303)
    resp = await _open(stranger, shared_agent, _source())
    assert resp.status_code == 404
    assert signed == []


# ── What the chat is told ───────────────────────────────────────────────────

async def _unreadable(tenant, agent):
    async with client_for(tenant) as c:
        resp = await c.get(f"/api/agents/{agent.id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["unreadable_knowledge_bases"]


async def test_the_chat_names_the_knowledge_bases_a_member_lacks(
    db: Session, _override_db, owner, member, shared_agent, store
):
    assert await _unreadable(owner, shared_agent) == []
    assert await _unreadable(member, shared_agent) == [INDEX]
    _grant(db, member, access.VECTOR_STORE, store.id, "read")
    assert await _unreadable(member, shared_agent) == []


# ── Sharing the agent with its knowledge bases ──────────────────────────────

def _kb_grant(db: Session, tenant, store_id: int):
    return db.query(AccessGrant).filter(
        AccessGrant.principal_id == tenant.account_id,
        AccessGrant.resource_type == access.VECTOR_STORE,
        AccessGrant.resource_id == store_id,
    ).first()


async def _share(owner, agent, member, **body):
    async with client_for(owner) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": member.account.email, **body})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_sharing_can_grant_view_access_to_the_knowledge_bases(
    db: Session, _override_db, owner, member, agent
):
    body = await _share(owner, agent, member, grantKnowledgeBases=True)
    assert body["knowledge_bases_granted"] == [INDEX]

    # The row predated nothing: the share created the VectorStore on demand.
    vs = db.query(VectorStore).filter(VectorStore.owner_account_id == owner.account_id,
                                      VectorStore.index_name == INDEX).one()
    assert _kb_grant(db, member, vs.id).role == "read"
    assert db.query(AccessGrantEvent).filter(
        AccessGrantEvent.resource_type == access.VECTOR_STORE,
        AccessGrantEvent.resource_id == vs.id,
        AccessGrantEvent.principal_id == member.account_id,
    ).count() == 1


async def test_sharing_without_the_option_grants_only_the_agent(
    db: Session, _override_db, owner, member, agent, store
):
    body = await _share(owner, agent, member)
    assert body["knowledge_bases_granted"] == []
    assert _kb_grant(db, member, store.id) is None


async def test_sharing_never_downgrades_a_write_grant(
    db: Session, _override_db, owner, member, agent, store
):
    _grant(db, member, access.VECTOR_STORE, store.id, "write")
    body = await _share(owner, agent, member, grantKnowledgeBases=True)
    assert body["knowledge_bases_granted"] == []
    assert _kb_grant(db, member, store.id).role == "write"


# ── The owner's audit ───────────────────────────────────────────────────────

async def _kb_audit(owner, agent):
    async with client_for(owner) as c:
        resp = await c.get(f"/api/access/resources/agent/{agent.id}/audit")
    assert resp.status_code == 200, resp.text
    [kb] = resp.json()["derived_exposure"]
    return kb


async def test_the_audit_names_agent_users_without_a_read_grant(
    db: Session, _override_db, owner, member, shared_agent, store
):
    kb = await _kb_audit(owner, shared_agent)
    assert (kb["label"], kb["missing_accounts"]) == (INDEX, [member.account.email])

    _grant(db, member, access.VECTOR_STORE, store.id, "read")
    assert (await _kb_audit(owner, shared_agent))["missing_accounts"] == []


# ── Backfill of shares made before the rule ─────────────────────────────────

def _migration():
    path = next(Path(__file__).parents[1].glob(
        "alembic/versions/d948d7bf156f_*.py"))
    spec = importlib.util.spec_from_file_location("kb_backfill", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_backfill_grants_read_to_existing_agent_users(db: Session, owner, member, agent):
    editor = make_tenant(db, slug="kb-access", account_id=9304, is_owner=False)
    outsider = make_tenant(db, slug="kb-access-elsewhere", account_id=9305)
    for t in (member, editor):
        _grant(db, t, access.AGENT, agent.id, "use")
    # A grant row naming someone outside the org (predates assert_same_org).
    db.add(AccessGrant(org_id=owner.org_id, principal_type=access.ACCOUNT,
                       principal_id=outsider.account_id, resource_type=access.AGENT,
                       resource_id=agent.id, role="use"))
    db.flush()

    mig = _migration()
    # No VectorStore row yet: the backfill creates it first.
    db.execute(text(mig.CREATE_MISSING_STORES))
    vs = db.query(VectorStore).filter(VectorStore.owner_account_id == owner.account_id,
                                      VectorStore.index_name == INDEX).one()
    assert vs.org_id == owner.org_id
    _grant(db, editor, access.VECTOR_STORE, vs.id, "write")

    db.execute(text(mig.GRANT_READ))
    db.expire_all()

    assert _kb_grant(db, member, vs.id).role == "read"
    assert _kb_grant(db, editor, vs.id).role == "write", "never downgraded"
    assert _kb_grant(db, outsider, vs.id) is None, "never crosses an org"
    assert _kb_grant(db, owner, vs.id) is None, "the owner needs none"
    events = db.query(AccessGrantEvent).filter(AccessGrantEvent.resource_id == vs.id).all()
    assert [(e.principal_id, e.role, e.org_id) for e in events] == [
        (member.account_id, "read", owner.org_id)
    ]

    # Idempotent: a second run adds nothing.
    db.execute(text(mig.CREATE_MISSING_STORES))
    db.execute(text(mig.GRANT_READ))
    assert db.query(AccessGrantEvent).filter(AccessGrantEvent.resource_id == vs.id).count() == 1
