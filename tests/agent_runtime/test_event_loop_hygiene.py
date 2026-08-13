"""Event-loop and connection-pool hygiene guards for the streaming path.

These pin the two invariants that keep long-lived SSE streams from starving
the rest of the service (critical once the agent runtime shares a process
with the CRUD API):

1. ``prepare_agent_context`` closes the request-scoped DB session before it
   returns — a stream must never pocket a pool connection for its lifetime.
2. CPU-bound PDF parsing (PyMuPDF) is dispatched via ``run_in_threadpool``,
   never run directly on the event loop.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from langchain_community.chat_message_histories import ChatMessageHistory

from src.agent_runtime import context as ctx_mod

SESSION_UUID = "12345678-1234-5678-1234-567812345678"

# Minimal code-defined agent config: no tools, no provider credential needed
# (get_required_credential_type is stubbed to None below), no contact scope.
OVERRIDE_CONFIG = {
    "data": {
        "systemPrompt": "You are a test assistant.",
        "model": {"provider": "test", "model": "test-model"},
        "tools": [],
    }
}


def _configured_db():
    """A MagicMock session that satisfies every query in the setup path."""
    db = MagicMock()
    chain = db.query.return_value.filter.return_value
    chain.scalar.return_value = 1  # Account.default_org_id
    # OrganizationMember membership check → truthy; ChatSession lookup → a
    # session row with a real id and no contact binding.
    chain.first.side_effect = [
        SimpleNamespace(id=99),                     # membership row
        SimpleNamespace(id=7, contact_id=None),     # chat session row
    ]
    chain.order_by.return_value.all.return_value = []  # chat history
    return db


@pytest.fixture
def stubbed_context(monkeypatch):
    """Stub the LLM/tool boundaries so setup runs without providers."""
    monkeypatch.setattr(ctx_mod, "get_required_credential_type", lambda provider: None)
    monkeypatch.setattr(ctx_mod, "create_llm", lambda **kw: (MagicMock(), None))

    async def fake_tools(**kw):
        return []

    monkeypatch.setattr(ctx_mod, "create_tools_from_agent_config", fake_tools)
    monkeypatch.setattr(ctx_mod, "build_message_history", lambda msgs: ChatMessageHistory())
    monkeypatch.setattr(ctx_mod, "extract_auth_token", lambda request, auth: None)


def _prepare(db, **overrides):
    """Run prepare_agent_context to completion (suite has no async plugin)."""
    kwargs = dict(
        agent_id=None,
        session_id=SESSION_UUID,
        prompt="hello",
        db=db,
        auth={"id": 1, "email": "test@example.com"},
        request=None,
        callbacks=[],
        agent_config_override=OVERRIDE_CONFIG,
    )
    kwargs.update(overrides)
    return asyncio.run(ctx_mod.prepare_agent_context(**kwargs))


def test_session_released_before_streaming(stubbed_context):
    """The request session must be closed by the time setup returns.

    FastAPI only closes dependency-injected sessions after the *response*
    completes — for SSE that is after the last token, ~30s later. If this
    close ever regresses, ~30 concurrent streams exhaust the 20+10 pool and
    every CRUD request on the instance starts 503ing on pool_timeout.
    """
    db = _configured_db()
    ctx = _prepare(db)

    db.close.assert_called_once()
    assert ctx.chat_session_id == 7  # captured before the close, still usable


def test_pdf_parse_runs_on_threadpool(stubbed_context, monkeypatch):
    """PDF parsing must go through run_in_threadpool, not the event loop."""
    dispatched = []

    async def spy_threadpool(func, *args, **kwargs):
        dispatched.append(func)
        return "sentinel-message"

    monkeypatch.setattr(ctx_mod, "run_in_threadpool", spy_threadpool)

    db = _configured_db()
    ctx = _prepare(db, pdf_base64="JVBERi0=", pdf_filename="a.pdf")

    assert dispatched == [ctx_mod.build_pdf_message]
    assert ctx.agent_input == "sentinel-message"


def test_no_pdf_skips_threadpool(stubbed_context, monkeypatch):
    """Plain text prompts never pay the threadpool dispatch."""
    dispatched = []

    async def spy_threadpool(func, *args, **kwargs):
        dispatched.append(func)
        return None

    monkeypatch.setattr(ctx_mod, "run_in_threadpool", spy_threadpool)

    db = _configured_db()
    ctx = _prepare(db)

    assert dispatched == []
    assert ctx.agent_input == "hello"
