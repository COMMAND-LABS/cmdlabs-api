"""knowledgeWrite: a note reaches the KB only through the approval queue.

Two halves, matching the code:
  - the TOOL (agent_runtime/tools/knowledge_write.py): built only for callers
    who may write to the KB, offers a closed topic list, and queues rather
    than writes;
  - the EXECUTOR (routers/tool_approvals/knowledge_write.py): on approval,
    renders the note and hands it to the existing upload+publish path.
"""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from jsonschema import ValidationError

from src.agent_runtime.tool_entitlement import TOOL_MODULES, allowed_tool_configs
from src.agent_runtime.tools import knowledge_write as tool_mod
from src.agent_runtime.tools.hitl_email_base import HITL_SENTINEL_KEY
from src.agent_runtime.tools.knowledge_write import create_knowledge_write_tool
from src.agent_runtime.tools.registry import ToolRegistry
from src.routers.tool_approvals import knowledge_write as exec_mod
from src.routers.tool_approvals.knowledge_write import execute_knowledge_write, render_note
from src.schemas import validate_against_schema

CFG = {
    "type": "knowledgeWrite",
    "provider": "pinecone",
    "index": "team-kb",
    "namespace": "logistics",
    "topics": ["tribal_knowledge", "business_processes", "tariff_changes"],
}


# ── Registration / entitlement ──────────────────────────────────────────────

def test_registered_and_gated_with_knowledge_bases():
    assert callable(ToolRegistry.get_builder("knowledgeWrite"))
    assert TOOL_MODULES["knowledgeWrite"] == "knowledge_bases"
    assert allowed_tool_configs([CFG], granted=set()) == []
    assert allowed_tool_configs([CFG], granted={"knowledge_bases"}) == [CFG]


# ── The tool ────────────────────────────────────────────────────────────────

async def test_owner_gets_the_tool_and_it_queues_an_approval():
    session = MagicMock()
    tool = await create_knowledge_write_tool(
        tool_config=CFG, account_id=1, db=MagicMock(),
        agent_owner_account_id=1, agent_id=42, chat_session_id_pk=99,
        session_factory=lambda: session,
    )
    assert tool.name == "knowledge_write"
    assert "tariff_changes" in tool.description

    # topic is a closed enum of the configured topics
    with pytest.raises(Exception):
        tool.args_schema(topic="random", text="x")
    tool.args_schema(topic="tariff_changes", text="Section 301 rate moves to 10% in March.")

    result = json.loads(await tool.coroutine(topic="tariff_changes", text="Rate moves to 10% in March."))
    assert result[HITL_SENTINEL_KEY] is True
    assert result["tool_type"] == "knowledgeWrite"
    assert result["preview"] == {"topic": "tariff_changes", "text": "Rate moves to 10% in March.",
                                 "index": "team-kb", "namespace": "logistics"}
    assert "not show up in search immediately" in result["message"]

    queued = session.add.call_args.args[0]
    assert queued.account_id == 1 and queued.agent_id == 42 and queued.chat_session_id == 99
    assert queued.payload == {"owner_account_id": 1, "index": "team-kb", "namespace": "logistics",
                              "topic": "tariff_changes", "text": "Rate moves to 10% in March."}
    session.commit.assert_called_once()


async def test_member_without_write_grant_does_not_get_the_tool(monkeypatch):
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="view-only")
    monkeypatch.setattr(tool_mod, "authorize_vector_store", deny)
    with pytest.raises(ValueError, match="may not write"):
        await create_knowledge_write_tool(tool_config=CFG, account_id=2, db=MagicMock(),
                                          agent_owner_account_id=1,
                                          org_scope=SimpleNamespace(org_id=5))


async def test_member_with_write_grant_is_checked_in_the_agents_org(monkeypatch):
    seen = {}
    def allow(db, caller, index, owner, *, require_write, org_id=None):
        seen.update(caller=caller, index=index, owner=owner, require_write=require_write, org_id=org_id)
        return owner
    monkeypatch.setattr(tool_mod, "authorize_vector_store", allow)
    await create_knowledge_write_tool(tool_config=CFG, account_id=2, db=MagicMock(),
                                      agent_owner_account_id=1, org_scope=SimpleNamespace(org_id=5))
    assert seen == {"caller": 2, "index": "team-kb", "owner": 1, "require_write": True, "org_id": 5}


@pytest.mark.parametrize("cfg", [
    {**CFG, "provider": "weaviate"},
    {**CFG, "topics": []},
    {k: v for k, v in CFG.items() if k != "namespace"},
])
async def test_misconfiguration_is_rejected_at_build(cfg):
    with pytest.raises(ValueError):
        await create_knowledge_write_tool(tool_config=cfg, account_id=1, db=MagicMock(),
                                          agent_owner_account_id=1)


# ── The executor ────────────────────────────────────────────────────────────

def test_render_note_front_matter_is_quoted():
    note = render_note(topic="tariff_changes", text="  rate: 10%  ", author_email="a@b.io",
                       approved_at=datetime(2026, 9, 26, tzinfo=timezone.utc))
    assert note.startswith('---\ntopic: "tariff_changes"\nauthor: "a@b.io"\n')
    assert 'approved_at: "2026-09-26T00:00:00+00:00"' in note
    assert note.endswith("---\n\nrate: 10%\n")


def _approval(**payload_overrides):
    payload = {"owner_account_id": 1, "index": "team-kb", "namespace": "logistics",
               "topic": "tariff_changes", "text": "Rate moves to 10%.", **payload_overrides}
    return SimpleNamespace(id=7, status="pending", payload=payload)


@pytest.fixture
def upload(monkeypatch):
    calls = []

    async def fake_upload(self, **kwargs):
        calls.append(kwargs)
        return {"success": True, "gcs_file_path": "vector_stores/x.md"}

    monkeypatch.setattr(exec_mod.VectorStoresUploadService, "upload_bytes_and_publish", fake_upload)
    monkeypatch.setattr(exec_mod, "authorize_vector_store", lambda *a, **k: 1)
    return calls


async def test_approval_stores_the_note_and_queues_txt_ingest(upload):
    approval, db = _approval(), MagicMock()
    message = await execute_knowledge_write(db, approval, account_id=2, user_email="ops@co.io", jwt="tok")

    assert approval.status == "approved" and db.commit.called
    assert "queued for ingestion" in message
    call = upload[0]
    assert call["topic_name"] == "txt-ingest-topic"
    assert call["index_name"] == "team-kb" and call["namespace"] == "logistics"
    assert call["account_id"] == 1, "stored in the KB OWNER's bucket"
    assert call["user_email"] == "ops@co.io" and call["jwt"] == "tok"
    assert call["filename"].startswith("tariff_changes-") and call["filename"].endswith(".md")
    body = call["file_bytes"].decode()
    assert 'topic: "tariff_changes"' in body and body.rstrip().endswith("Rate moves to 10%.")


async def test_user_edit_overrides_the_agents_text(upload):
    await execute_knowledge_write(MagicMock(), _approval(), account_id=1, user_email="u@co.io",
                                  jwt=None, text_override="  Edited by a human.  ")
    assert upload[0]["file_bytes"].decode().rstrip().endswith("Edited by a human.")


async def test_revoked_access_blocks_at_approval_time(monkeypatch, upload):
    def deny(*a, **k):
        raise HTTPException(status_code=403, detail="view-only")
    monkeypatch.setattr(exec_mod, "authorize_vector_store", deny)
    approval = _approval()
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_write(MagicMock(), approval, account_id=2, user_email="u", jwt=None)
    assert exc.value.status_code == 403 and approval.status == "pending" and not upload


@pytest.mark.parametrize("payload", [{"text": "   "}, {"topic": "Bad Topic"}, {"index": None}])
async def test_bad_payload_is_a_422(upload, payload):
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_write(MagicMock(), _approval(**payload), account_id=1, user_email="u", jwt=None)
    assert exc.value.status_code == 422


async def test_upload_failure_leaves_the_approval_pending(monkeypatch, upload):
    async def failing(self, **kwargs):
        return {"success": False, "error": "boom"}
    monkeypatch.setattr(exec_mod.VectorStoresUploadService, "upload_bytes_and_publish", failing)
    approval = _approval()
    with pytest.raises(HTTPException) as exc:
        await execute_knowledge_write(MagicMock(), approval, account_id=1, user_email="u", jwt=None)
    assert exc.value.status_code == 500 and approval.status == "pending"


# ── Schema ──────────────────────────────────────────────────────────────────

def _config(tools: list) -> dict:
    return {"schema": "agent_config", "version": 4,
            "data": {"systemPrompt": "You are helpful.", "tools": tools}}


def test_schema_accepts_knowledge_write():
    validate_against_schema(_config([CFG]), "agent_config", 4)


@pytest.mark.parametrize("bad", [
    {**CFG, "topics": []},
    {**CFG, "topics": ["Tariff Changes"]},
    {**CFG, "topics": ["a", "a"]},
    {k: v for k, v in CFG.items() if k != "topics"},
])
def test_schema_rejects_bad_knowledge_write(bad):
    with pytest.raises(ValidationError):
        validate_against_schema(_config([bad]), "agent_config", 4)
