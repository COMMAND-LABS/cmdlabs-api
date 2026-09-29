"""knowledgeDelete: nothing leaves a knowledge base until a person approves.

Two halves, matching the code:
  - the TOOL (agent_runtime/tools/knowledge_delete.py): built only for callers
    who may write to the KB, looks the target up so the approver sees the real
    text, and queues rather than deletes;
  - the EXECUTOR (routers/tool_approvals/knowledge_delete.py): on approval,
    deletes and logs one per-file DELETE per document it touched.
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from jsonschema import ValidationError

from src.agent_runtime.tool_entitlement import TOOL_MODULES, allowed_tool_configs
from src.agent_runtime.tools import knowledge_delete as tool_mod
from src.agent_runtime.tools.hitl_email_base import HITL_SENTINEL_KEY
from src.agent_runtime.tools.knowledge_delete import create_knowledge_delete_tool
from src.agent_runtime.tools.registry import ToolRegistry
from src.db.models import VectorDbIngestionLog
from src.routers.tool_approvals import knowledge_delete as exec_mod
from src.routers.tool_approvals.knowledge_delete import execute_knowledge_delete
from src.schemas import validate_against_schema

CFG = {"type": "knowledgeDelete", "provider": "pinecone", "index": "team-kb", "namespace": "logistics"}

VECTORS = {
    "v1": {"filename": "note-a.md", "content": "Broker cutoff is Thursday noon."},
    "v2": {"filename": "note-a.md", "content": "Second passage of note A."},
    "v3": {"filename": "faq.csv", "content": "Q: Who files ISF? A: Our broker."},
}


class FakeIndex:
    def __init__(self, vectors=VECTORS):
        self.vectors = dict(vectors)
        self.deleted = []

    def fetch(self, ids, namespace):
        return {"vectors": {i: {"metadata": self.vectors[i]} for i in ids if i in self.vectors}}

    def delete(self, ids, namespace):
        self.deleted.extend(ids)


def _by_filename(index, namespace, filename, cap=None):
    return [i for i, m in index.vectors.items() if m["filename"] == filename], False


# ── Registration / entitlement / schema ─────────────────────────────────────

def test_registered_and_gated_with_knowledge_bases():
    assert callable(ToolRegistry.get_builder("knowledgeDelete"))
    assert TOOL_MODULES["knowledgeDelete"] == "knowledge_bases"
    assert allowed_tool_configs([CFG], granted=set()) == []
    assert allowed_tool_configs([CFG], granted={"knowledge_bases"}) == [CFG]


def _config(tools):
    return {"schema": "agent_config", "version": 4,
            "data": {"systemPrompt": "You are helpful.", "tools": tools}}


def test_schema():
    validate_against_schema(_config([CFG]), "agent_config", 4)
    with pytest.raises(ValidationError):
        validate_against_schema(_config([{k: v for k, v in CFG.items() if k != "index"}]),
                                "agent_config", 4)


# ── The tool ────────────────────────────────────────────────────────────────

@pytest.fixture
def index(monkeypatch):
    idx = FakeIndex()
    monkeypatch.setattr(tool_mod, "authorize_vector_store", lambda *a, **k: 1)
    monkeypatch.setattr(tool_mod, "load_pinecone_index", lambda *a, **k: (idx, "logistics", "team-kb"))
    monkeypatch.setattr(tool_mod, "collect_ids_for_filename", _by_filename)
    return idx


async def _tool(session=None, **kw):
    return await create_knowledge_delete_tool(
        tool_config=CFG, account_id=1, db=MagicMock(), agent_owner_account_id=1,
        agent_id=42, chat_session_id_pk=99, session_factory=lambda: session or MagicMock(), **kw)


async def test_whole_document_is_queued_with_its_text(index):
    session = MagicMock()
    tool = await _tool(session)
    result = json.loads(await tool.coroutine(reason="Cutoff moved to Friday", filename="note-a.md"))

    assert result[HITL_SENTINEL_KEY] is True and result["tool_type"] == "knowledgeDelete"
    assert result["preview"]["passage_count"] == 2
    assert [p["text"] for p in result["preview"]["passages"]] == [
        "Broker cutoff is Thursday noon.", "Second passage of note A."]
    queued = session.add.call_args.args[0]
    assert queued.payload == {"owner_account_id": 1, "index": "team-kb", "namespace": "logistics",
                              "reason": "Cutoff moved to Friday", "filename": "note-a.md"}
    assert index.deleted == [], "the tool never deletes"


async def test_passages_are_queued_and_unknown_ids_left_out(index):
    session = MagicMock()
    tool = await _tool(session)
    result = json.loads(await tool.coroutine(reason="wrong", passage_ids=["v3", "gone"]))
    assert session.add.call_args.args[0].payload["passage_ids"] == ["v3"]
    assert result["preview"]["passages"][0]["filename"] == "faq.csv"
    assert "gone" in result["message"]


@pytest.mark.parametrize("args, error", [
    ({"filename": "nope.md"}, "No document named"),
    ({"passage_ids": ["x", "y"]}, "None of those passages"),
    ({"filename": "note-a.md", "passage_ids": ["v1"]}, "not both"),
    ({}, "not both"),
])
async def test_bad_targets_are_errors_not_approvals(index, args, error):
    session = MagicMock()
    tool = await _tool(session)
    result = json.loads(await tool.coroutine(reason="r", **args))
    assert error in result["error"] and not session.add.called


async def test_member_without_write_grant_does_not_get_the_tool(monkeypatch):
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="view-only")
    monkeypatch.setattr(tool_mod, "authorize_vector_store", deny)
    with pytest.raises(ValueError, match="may not write"):
        await create_knowledge_delete_tool(tool_config=CFG, account_id=2, db=MagicMock(),
                                           agent_owner_account_id=1,
                                           org_scope=SimpleNamespace(org_id=5))


# ── The executor ────────────────────────────────────────────────────────────

def _approval(**payload):
    base = {"owner_account_id": 1, "index": "team-kb", "namespace": "logistics", "reason": "old"}
    return SimpleNamespace(id=7, status="pending", payload={**base, **payload})


@pytest.fixture
def pinecone(monkeypatch):
    idx = FakeIndex()
    monkeypatch.setattr(exec_mod, "authorize_vector_store", lambda *a, **k: 1)
    monkeypatch.setattr(exec_mod, "get_pinecone_api_key_for_index", lambda *a: "key")
    monkeypatch.setattr(exec_mod, "Pinecone", lambda api_key: SimpleNamespace(Index=lambda name: idx))
    monkeypatch.setattr(exec_mod, "collect_ids_for_filename", _by_filename)
    monkeypatch.setattr(exec_mod, "invalidate_namespace_cache", lambda *a: None)
    return idx


def _logs(db):
    return [c.args[0] for c in db.add.call_args_list if isinstance(c.args[0], VectorDbIngestionLog)]


async def test_approving_a_document_deletes_it_and_logs_a_per_file_delete(pinecone):
    approval, db = _approval(filename="note-a.md"), MagicMock()
    message = await execute_knowledge_delete(db, approval, account_id=2, user_email="ops@co.io")

    assert sorted(pinecone.deleted) == ["v1", "v2"]
    assert approval.status == "approved" and "Removed 'note-a.md'" in message
    [row] = _logs(db)
    assert row.account_id == 1 and row.filenames == ["note-a.md"], \
        "a DELETE without filenames would read as the whole section being wiped"
    assert (row.operation_type, row.status, row.vectors_deleted) == ("DELETE", "SUCCESS", 2)


async def test_approving_passages_logs_each_document_it_touched(pinecone):
    approval, db = _approval(passage_ids=["v1", "v3", "gone"]), MagicMock()
    await execute_knowledge_delete(db, approval, account_id=1, user_email="u")
    assert sorted(pinecone.deleted) == ["v1", "v3"]
    assert sorted((r.filenames[0], r.vectors_deleted) for r in _logs(db)) == [
        ("faq.csv", 1), ("note-a.md", 1)]


async def test_revoked_access_blocks_at_approval_time(monkeypatch, pinecone):
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="view-only")
    monkeypatch.setattr(exec_mod, "authorize_vector_store", deny)
    approval = _approval(filename="note-a.md")
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_delete(MagicMock(), approval, account_id=2, user_email="u")
    assert exc.value.status_code == 403 and approval.status == "pending" and not pinecone.deleted


async def test_pinecone_failure_leaves_the_approval_pending(monkeypatch, pinecone):
    def boom(*a, **k):
        raise RuntimeError("pinecone down")
    monkeypatch.setattr(pinecone, "delete", boom)
    approval = _approval(filename="note-a.md")
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_delete(MagicMock(), approval, account_id=1, user_email="u")
    assert exc.value.status_code == 500 and approval.status == "pending"


@pytest.mark.parametrize("payload", [
    {},                                              # neither target
    {"filename": "a.md", "passage_ids": ["v1"]},     # both
    {"passage_ids": [""]},
    {"filename": "a.md", "index": None},
])
async def test_bad_payload_is_a_422(pinecone, payload):
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_delete(MagicMock(), _approval(**payload), account_id=1, user_email="u")
    assert exc.value.status_code == 422
