"""Tests for switching the agent on a chat session (PATCH /sessions/{id}).

The session and its transcript stay; only the agent answering the next turn
changes. Access to the new agent is gated exactly like session creation, and
the PATCH is a real partial update: an agent-only patch must not clear the
title (the handler used to overwrite title unconditionally), and a title-only
patch must not touch the agent.

Agents are seeded straight into the DB, not through POST /api/agents/ — the
rate limiter is one in-memory counter for the whole suite (see
test_app_settings.py).
"""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, Agent, ChatSession, Contact
from tests.conftest import ROOT_ORG_ID
from tests.org_isolation import client_for, make_tenant

SESSIONS_URL = "/api/chat-sessions/sessions"

VALID_AGENT_CONFIG = {
    "schema": "agent_config",
    "version": 4,
    "data": {
        "systemPrompt": "You are a test assistant.",
        "model": {"provider": "openai", "model": "gpt-4o-mini"},
        "tools": [],
    },
}


def _seed_agent(db: Session, *, account_id: int, org_id: int = ROOT_ORG_ID,
                name: str = "Session Agent") -> Agent:
    agent = Agent(org_id=org_id, account_id=account_id, name=name,
                  config=VALID_AGENT_CONFIG)
    db.add(agent)
    db.flush()
    return agent


def _seed_session(db: Session, *, account_id: int, agent_id: int | None = None,
                  title: str | None = None,
                  contact_id: int | None = None) -> ChatSession:
    session = ChatSession(
        session_id=str(uuid.uuid4()),
        account_id=account_id,
        agent_id=agent_id,
        title=title,
        contact_id=contact_id,
    )
    db.add(session)
    db.flush()
    return session


async def test_switch_agent_keeps_session_and_title(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    old_agent = _seed_agent(db, account_id=test_account.id, name="Old Agent")
    new_agent = _seed_agent(db, account_id=test_account.id, name="New Agent")
    session = _seed_session(db, account_id=test_account.id,
                            agent_id=old_agent.id, title="Long-running thread")

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{session.session_id}",
        json={"agentId": new_agent.id},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["agentId"] == new_agent.id
    assert body["sessionId"] == str(session.session_id)
    # The agent-only patch must not have cleared the title.
    assert body["title"] == "Long-running thread"

    db.refresh(session)
    assert session.agent_id == new_agent.id
    assert session.title == "Long-running thread"


async def test_title_only_patch_keeps_agent(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    agent = _seed_agent(db, account_id=test_account.id)
    session = _seed_session(db, account_id=test_account.id, agent_id=agent.id)

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{session.session_id}",
        json={"title": "Renamed"},
    )

    assert resp.status_code == 200
    assert resp.json()["agentId"] == agent.id


async def test_switching_to_null_agent_is_refused(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    agent = _seed_agent(db, account_id=test_account.id)
    session = _seed_session(db, account_id=test_account.id, agent_id=agent.id)

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{session.session_id}",
        json={"agentId": None},
    )

    assert resp.status_code == 400
    db.refresh(session)
    assert session.agent_id == agent.id


async def test_switching_to_unknown_agent_is_refused(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    agent = _seed_agent(db, account_id=test_account.id)
    session = _seed_session(db, account_id=test_account.id, agent_id=agent.id)

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{session.session_id}",
        json={"agentId": 999999},
    )

    assert resp.status_code == 403
    db.refresh(session)
    assert session.agent_id == agent.id


async def test_cannot_switch_to_another_orgs_agent(db, _override_db):
    """The switch is gated like creation: no cross-tenant agent reach."""
    acme = make_tenant(db, slug="switch-acme", account_id=9401)
    rival = make_tenant(db, slug="switch-rival", account_id=9402)
    own_agent = _seed_agent(db, account_id=rival.account_id,
                            org_id=rival.org_id, name="Rival Agent")
    foreign_agent = _seed_agent(db, account_id=acme.account_id,
                                org_id=acme.org_id, name="Acme Agent")
    session = _seed_session(db, account_id=rival.account_id,
                            agent_id=own_agent.id)
    db.commit()

    async with client_for(rival) as rival_client:
        resp = await rival_client.patch(
            f"{SESSIONS_URL}/{session.session_id}",
            json={"agentId": foreign_agent.id},
        )

    assert resp.status_code == 403
    db.refresh(session)
    assert session.agent_id == own_agent.id


async def test_contact_bound_session_cannot_switch(
    authed_client: AsyncClient, db: Session, test_account: Account
):
    """The contact binding is a server-trusted tool scope; re-pointing the
    session at an arbitrary agent would silently drop it."""
    contact = Contact(org_id=ROOT_ORG_ID, account_id=test_account.id,
                      first_name="Scoped", email="scoped@example.com")
    db.add(contact)
    db.flush()
    agent = _seed_agent(db, account_id=test_account.id)
    session = _seed_session(db, account_id=test_account.id,
                            contact_id=contact.id)

    resp = await authed_client.patch(
        f"{SESSIONS_URL}/{session.session_id}",
        json={"agentId": agent.id},
    )

    assert resp.status_code == 400
    db.refresh(session)
    assert session.agent_id is None
