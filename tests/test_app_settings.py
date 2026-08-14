"""Tests for /api/app-settings — per-account, per-org application preferences.

The default agent points at an org-scoped resource, so the interesting cases
are the boundary ones: an agent in another org must be rejected on write and
masked on read, and a deleted agent must clear the default (FK SET NULL)
rather than serve a dangling id.

Agents are seeded straight into the DB, not through POST /api/agents/: the
rate limiter is one in-memory counter for the whole suite, and this file
going through the API was what tipped the agent-create budget over its
10/minute limit (as the 36th test, everything runs inside one clock minute).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, Agent
from tests.conftest import ROOT_ORG_ID
from tests.org_isolation import Tenant, client_for, make_tenant

VALID_AGENT_CONFIG = {
    "schema": "agent_config",
    "version": 4,
    "data": {
        "systemPrompt": "You are a test assistant.",
        "model": {"provider": "openai", "model": "gpt-4o-mini"},
        "tools": [],
    },
}


def _seed_agent(db: Session, *, account_id: int, org_id: int,
                name: str = "Settings Agent") -> int:
    agent = Agent(org_id=org_id, account_id=account_id, name=name,
                  config=VALID_AGENT_CONFIG)
    db.add(agent)
    db.flush()
    return agent.id


@pytest.fixture()
def agent_id(db: Session, test_account: Account) -> int:
    """An agent owned by the authed_client's account, in its org."""
    return _seed_agent(db, account_id=test_account.id, org_id=ROOT_ORG_ID)


async def test_get_before_any_save_returns_nulls(authed_client: AsyncClient):
    resp = await authed_client.get("/api/app-settings/")
    assert resp.status_code == 200
    assert resp.json() == {"default_agent_id": None, "elevenlabs_voice_id": None}


async def test_settings_roundtrip(authed_client: AsyncClient, agent_id: int):
    put_resp = await authed_client.put(
        "/api/app-settings/",
        json={"default_agent_id": agent_id, "elevenlabs_voice_id": "voice-123"},
    )
    assert put_resp.status_code == 200
    assert put_resp.json() == {
        "default_agent_id": agent_id,
        "elevenlabs_voice_id": "voice-123",
    }

    get_resp = await authed_client.get("/api/app-settings/")
    assert get_resp.status_code == 200
    assert get_resp.json() == {
        "default_agent_id": agent_id,
        "elevenlabs_voice_id": "voice-123",
    }


async def test_partial_update_leaves_other_fields_alone(
    authed_client: AsyncClient, agent_id: int
):
    await authed_client.put(
        "/api/app-settings/",
        json={"default_agent_id": agent_id, "elevenlabs_voice_id": "voice-123"},
    )

    resp = await authed_client.put(
        "/api/app-settings/", json={"elevenlabs_voice_id": "voice-456"}
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "default_agent_id": agent_id,
        "elevenlabs_voice_id": "voice-456",
    }


async def test_explicit_null_clears_default_agent(
    authed_client: AsyncClient, agent_id: int
):
    await authed_client.put("/api/app-settings/", json={"default_agent_id": agent_id})

    resp = await authed_client.put(
        "/api/app-settings/", json={"default_agent_id": None}
    )
    assert resp.status_code == 200
    assert resp.json()["default_agent_id"] is None


async def test_empty_update_is_refused(authed_client: AsyncClient):
    resp = await authed_client.put("/api/app-settings/", json={})
    assert resp.status_code == 400


async def test_unknown_agent_is_404(authed_client: AsyncClient):
    resp = await authed_client.put(
        "/api/app-settings/", json={"default_agent_id": 999999}
    )
    assert resp.status_code == 404


async def test_deleting_the_agent_clears_the_default(
    authed_client: AsyncClient, agent_id: int
):
    """End to end: deleting the agent through the API clears the default."""
    await authed_client.put("/api/app-settings/", json={"default_agent_id": agent_id})

    del_resp = await authed_client.delete(f"/api/agents/{agent_id}")
    assert del_resp.status_code in (200, 204)

    get_resp = await authed_client.get("/api/app-settings/")
    assert get_resp.status_code == 200
    assert get_resp.json()["default_agent_id"] is None


async def test_unauthenticated_is_refused(client: AsyncClient):
    resp = await client.get("/api/app-settings/")
    assert resp.status_code == 401


async def test_cannot_default_to_another_orgs_agent(db, _override_db):
    """Setting a default must not leak existence across the tenant boundary."""
    acme = make_tenant(db, slug="appset-acme", account_id=9301)
    rival = make_tenant(db, slug="appset-rival", account_id=9302)
    agent_id = _seed_agent(db, account_id=acme.account_id, org_id=acme.org_id,
                           name="Acme Agent")
    db.commit()

    async with client_for(rival) as rival_client:
        resp = await rival_client.put(
            "/api/app-settings/", json={"default_agent_id": agent_id}
        )
        assert resp.status_code == 404

        # And rival's own settings stay untouched by acme's world.
        get_resp = await rival_client.get("/api/app-settings/")
        assert get_resp.json()["default_agent_id"] is None


async def test_settings_are_scoped_per_account(db, _override_db):
    """Two members of the same org each hold their own default."""
    first = make_tenant(db, slug="appset-team", account_id=9303)
    second = make_tenant(db, slug="appset-team", account_id=9304, is_owner=False)
    agent_id = _seed_agent(db, account_id=first.account_id, org_id=first.org_id,
                           name="Shared Org Agent")
    db.commit()

    async with client_for(first) as first_client:
        put_resp = await first_client.put(
            "/api/app-settings/", json={"default_agent_id": agent_id}
        )
        assert put_resp.status_code == 200

    async with client_for(second) as second_client:
        resp = await second_client.get("/api/app-settings/")
        assert resp.status_code == 200
        assert resp.json()["default_agent_id"] is None
