"""Tests for renaming a chat session (PATCH /sessions/{session_id}).

Sessions are created untitled, so every list can only show "Agent #<id>" until
a title is set. Covers:
- A rename persists and is returned.
- Blank titles clear the field to NULL (so the label falls back) rather than
  saving an empty string.
- Another account's session is not renameable (404, no existence leak).
- An unknown / malformed session id is rejected.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, ChatSession

SESSIONS_URL = "/api/chat-sessions/sessions"


@pytest.fixture()
def owned_session(db: Session, test_account: Account) -> ChatSession:
    session = ChatSession(
        session_id=str(uuid.uuid4()),
        account_id=test_account.id,
    )
    db.add(session)
    db.flush()
    return session


@pytest.fixture()
def foreign_session(db: Session) -> ChatSession:
    """A session owned by a *different* account."""
    other = Account(id=2, email="other@example.com")
    db.add(other)
    db.flush()
    session = ChatSession(
        session_id=str(uuid.uuid4()),
        account_id=other.id,
        title="not yours",
    )
    db.add(session)
    db.flush()
    return session


async def test_rename_session_persists_title(
    authed_client: AsyncClient, db: Session, owned_session: ChatSession
):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{owned_session.session_id}",
        json={"title": "Lead scoring run"},
    )

    assert resp.status_code == 200
    assert resp.json()["title"] == "Lead scoring run"

    db.refresh(owned_session)
    assert owned_session.title == "Lead scoring run"


async def test_rename_session_trims_whitespace(
    authed_client: AsyncClient, owned_session: ChatSession
):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{owned_session.session_id}",
        json={"title": "  Q3 pipeline review  "},
    )

    assert resp.status_code == 200
    assert resp.json()["title"] == "Q3 pipeline review"


async def test_blank_title_clears_back_to_null(
    authed_client: AsyncClient, db: Session, owned_session: ChatSession
):
    owned_session.title = "Old name"
    db.flush()

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{owned_session.session_id}",
        json={"title": "   "},
    )

    # NULL, not "" — an empty string would render as a nameless row with no
    # way back to the "Agent #<id>" fallback.
    assert resp.status_code == 200
    assert resp.json()["title"] is None

    db.refresh(owned_session)
    assert owned_session.title is None


async def test_rename_foreign_session_returns_404(
    authed_client: AsyncClient, db: Session, foreign_session: ChatSession
):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{foreign_session.session_id}",
        json={"title": "hijacked"},
    )

    # 404 (not 403) so we never leak the existence of another account's session.
    assert resp.status_code == 404

    db.refresh(foreign_session)
    assert foreign_session.title == "not yours"


async def test_rename_unknown_session_returns_404(authed_client: AsyncClient):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{uuid.uuid4()}", json={"title": "ghost"}
    )

    assert resp.status_code == 404


async def test_rename_malformed_session_id_returns_400(authed_client: AsyncClient):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/not-a-uuid", json={"title": "nope"}
    )

    assert resp.status_code == 400


async def test_title_over_max_length_is_rejected(
    authed_client: AsyncClient, owned_session: ChatSession
):
    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{owned_session.session_id}",
        json={"title": "x" * 201},
    )

    assert resp.status_code == 422
