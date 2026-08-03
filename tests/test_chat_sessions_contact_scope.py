"""Layer A tests: chat-session <-> contact binding and its ownership gate.

Covers:
- A session can be bound to a contact the caller owns (contact_id persisted).
- The ownership gate: binding to another account's contact -> 404.
- Unbound sessions still work (contact_id is None).
- get_sessions hides contact-bound sessions by default (H1) and returns
  them only when an explicit contact_id is requested.
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, Contact, ChatSession

SESSIONS_URL = "/api/chat-sessions/sessions"


@pytest.fixture()
def owned_contact(db: Session, test_account: Account) -> Contact:
    contact = Contact(
        account_id=test_account.id,
        first_name="Rodolfo",
        last_name="Capdevilla",
        email="rodolfo.owned@example.com",
    )
    db.add(contact)
    db.flush()
    return contact


@pytest.fixture()
def foreign_contact(db: Session) -> Contact:
    """A contact owned by a *different* account."""
    other = Account(id=2, email="other@example.com")
    db.add(other)
    db.flush()
    contact = Contact(
        account_id=other.id,
        first_name="Someone",
        last_name="Else",
        email="someone.else@example.com",
    )
    db.add(contact)
    db.flush()
    return contact


async def test_create_session_with_owned_contact_binds_it(
    authed_client: AsyncClient, db: Session, owned_contact: Contact
):
    resp = await authed_client.post(SESSIONS_URL, json={"contactId": owned_contact.id})

    assert resp.status_code == 201
    body = resp.json()
    assert body["contactId"] == owned_contact.id
    assert body["agentId"] is None

    row = db.query(ChatSession).filter(ChatSession.id == body["id"]).first()
    assert row is not None and row.contact_id == owned_contact.id


async def test_create_session_with_foreign_contact_returns_404(
    authed_client: AsyncClient, foreign_contact: Contact
):
    resp = await authed_client.post(SESSIONS_URL, json={"contactId": foreign_contact.id})

    # 404 (not 403) so we never leak the existence of another account's contact.
    assert resp.status_code == 404


async def test_create_session_without_contact_is_unbound(
    authed_client: AsyncClient,
):
    resp = await authed_client.post(SESSIONS_URL, json={})

    assert resp.status_code == 201
    assert resp.json()["contactId"] is None


async def test_get_sessions_hides_contact_bound_by_default(
    authed_client: AsyncClient, owned_contact: Contact
):
    await authed_client.post(SESSIONS_URL, json={"title": "general chat"})
    await authed_client.post(SESSIONS_URL, json={"contactId": owned_contact.id})

    resp = await authed_client.get(SESSIONS_URL)
    assert resp.status_code == 200
    body = resp.json()
    sessions = body["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["contactId"] is None
    assert sessions[0]["title"] == "general chat"
    # The count must reflect the same scoping as the rows: the contact-bound
    # session is excluded from `total`, not just from the returned page.
    assert body["total"] == 1
    assert body["has_more"] is False


async def test_get_sessions_returns_contact_bound_when_filtered(
    authed_client: AsyncClient, owned_contact: Contact
):
    await authed_client.post(SESSIONS_URL, json={"title": "general chat"})
    await authed_client.post(SESSIONS_URL, json={"contactId": owned_contact.id})

    resp = await authed_client.get(SESSIONS_URL, params={"contact_id": owned_contact.id})
    assert resp.status_code == 200
    body = resp.json()
    sessions = body["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["contactId"] == owned_contact.id
    assert body["total"] == 1


async def test_get_sessions_paginates_with_total_and_has_more(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    """The envelope carries the pre-pagination total so the UI can render
    numbered pages ("51–75 of N") instead of guessing from the page length."""
    # Seeded directly rather than via POST: create_session is rate-limited to
    # 10/minute and the suite shares one client address.
    for i in range(5):
        db.add(
            ChatSession(
                session_id=str(uuid.uuid4()),
                account_id=test_account.id,
                title=f"session {i}",
            )
        )
    db.flush()

    resp = await authed_client.get(SESSIONS_URL, params={"limit": 2, "offset": 0})
    assert resp.status_code == 200
    first = resp.json()
    assert len(first["sessions"]) == 2
    assert first["total"] == 5
    assert first["limit"] == 2
    assert first["offset"] == 0
    assert first["has_more"] is True

    resp = await authed_client.get(SESSIONS_URL, params={"limit": 2, "offset": 4})
    assert resp.status_code == 200
    last = resp.json()
    assert len(last["sessions"]) == 1
    assert last["total"] == 5
    assert last["has_more"] is False
