"""Tests for /api/memory-chat — the context-window teaching demo.

The two claims under test are the two halves of the lesson:
- PERSISTENCE: prompts are stored before the model is even consulted, so a
  turn survives a missing credential, and the transcript is org-isolated.
- THE WINDOW: only the newest turns fitting in HALF the context limit are
  "in window"; older rows are excluded from the model's view but never
  deleted. compute_window is exercised directly for the arithmetic.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, MemoryChatMessage
from src.routers.memory_chat.router import compute_window, estimate_tokens
from tests.conftest import ROOT_ORG_ID
from tests.org_isolation import client_for, make_tenant

URL = "/api/memory-chat/"


class FakeMessage:
    def __init__(self, content: str):
        self.content = content


def test_compute_window_drops_oldest_first():
    # 4 chars ≈ 1 token. Budget = 200 // 2 = 100 tokens = ~400 chars.
    messages = [FakeMessage("x" * 400) for _ in range(3)]
    # Only the newest fits: 100 tokens each, budget 100.
    assert compute_window(messages, 200) == 2


def test_compute_window_keeps_everything_that_fits():
    messages = [FakeMessage("x" * 40) for _ in range(5)]  # 10 tokens each
    assert compute_window(messages, 2_000) == 0  # budget 1000 ≫ 50


def test_compute_window_when_even_newest_does_not_fit():
    messages = [FakeMessage("x" * 4_000)]  # 1000 tokens, budget 100
    assert compute_window(messages, 200) == 1


def test_estimate_tokens_floor():
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd" * 25) == 25


def _seed(db: Session, account_id: int, org_id: int, *contents: str):
    rows = []
    for i, content in enumerate(contents):
        row = MemoryChatMessage(
            account_id=account_id, org_id=org_id,
            role="human" if i % 2 == 0 else "ai", content=content,
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


async def test_empty_transcript(authed_client: AsyncClient):
    resp = await authed_client.get(URL)
    assert resp.status_code == 200
    body = resp.json()
    assert body["messages"] == []
    assert body["window"]["dropped_count"] == 0
    assert body["window"]["used_tokens"] == 0


async def test_transcript_marks_dropped_messages(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    # Three ~100-token messages against a 2000-token limit (budget 1000):
    # all fit. Against the same data the arithmetic is checked end to end.
    _seed(db, test_account.id, ROOT_ORG_ID,
          "x" * 400, "y" * 400, "z" * 400)

    resp = await authed_client.get(URL, params={"context_limit": 2000})
    assert resp.status_code == 200
    body = resp.json()
    assert [m["in_window"] for m in body["messages"]] == [True, True, True]
    assert body["window"]["used_tokens"] == 300
    assert body["window"]["budget"] == 1000

    # Shrink the window: budget 1000 → 2000-token limit was roomy, a
    # 2000//2=1000... use the smallest limit: 2000. Instead seed longer rows.
    _seed(db, test_account.id, ROOT_ORG_ID, "w" * 3000)  # 750 tokens

    resp = await authed_client.get(URL, params={"context_limit": 2000})
    body = resp.json()
    # Newest (750) + z (100) + y (100) = 950 ≤ 1000; x (100) would break it.
    assert [m["in_window"] for m in body["messages"]] == [
        False, True, True, True,
    ]
    assert body["window"]["dropped_count"] == 1
    assert body["window"]["used_tokens"] == 950


async def test_unknown_context_limit_falls_back_to_default(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    _seed(db, test_account.id, ROOT_ORG_ID, "hello")
    resp = await authed_client.get(URL, params={"context_limit": 123})
    assert resp.status_code == 200
    assert resp.json()["window"]["context_limit"] == 4000


async def test_prompt_is_persisted_even_without_a_credential(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    """The stream yields an in-band error frame (no API key), but the human
    turn must already be a row — persistence does not depend on the model."""
    resp = await authed_client.post(
        f"{URL}stream",
        json={"prompt": "remember me", "provider": "anthropic",
              "model": "claude-haiku-4-5", "context_limit": 2000},
    )
    assert resp.status_code == 200
    assert "error" in resp.text  # in-band SSE error frame

    rows = db.query(MemoryChatMessage).filter(
        MemoryChatMessage.account_id == test_account.id).all()
    assert [(r.role, r.content) for r in rows] == [("human", "remember me")]


async def test_clear_deletes_only_own_transcript(db, _override_db):
    acme = make_tenant(db, slug="memchat-acme", account_id=9501)
    rival = make_tenant(db, slug="memchat-rival", account_id=9502)
    _seed(db, acme.account_id, acme.org_id, "acme says hi")
    _seed(db, rival.account_id, rival.org_id, "rival says hi")
    db.commit()

    async with client_for(acme) as acme_client:
        # Isolation on read: acme sees only its own row.
        resp = await acme_client.get(URL)
        assert [m["content"] for m in resp.json()["messages"]] == ["acme says hi"]

        del_resp = await acme_client.delete(URL)
        assert del_resp.status_code == 204

    async with client_for(rival) as rival_client:
        resp = await rival_client.get(URL)
        assert [m["content"] for m in resp.json()["messages"]] == ["rival says hi"]


async def test_unauthenticated_is_refused(client: AsyncClient):
    resp = await client.get(URL)
    assert resp.status_code == 401
