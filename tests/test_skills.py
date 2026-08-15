"""Tests for the /api/skills endpoints (CRUD cycle) and agent attachment."""

import pytest
from httpx import AsyncClient

from src.rate_limit import limiter


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The limiter is one in-memory counter for the whole suite (see
    test_app_settings.py's header). This file exercises validation paths, so
    it necessarily makes many requests against the 10/minute write budget —
    resetting per test keeps it honest without starving the files that run
    after this one."""
    limiter.reset()
    yield

SKILL_MD = """---
name: brand-voice
description: How to write in the company voice.
---
# Brand voice

Always write plainly. Avoid superlatives.
"""

VALID_AGENT_CONFIG = {
    "schema": "agent_config",
    "version": 4,
    "data": {
        "systemPrompt": "You are a test assistant.",
        "model": {"provider": "openai", "model": "gpt-4o-mini"},
        "tools": [],
    },
}


async def _create_skill(client: AsyncClient, **overrides) -> dict:
    body = {
        "name": "test-skill",
        "description": "A skill for testing.",
        "content": "# Do the thing\n\nStep by step.",
    }
    body.update(overrides)
    resp = await client.post("/api/skills/", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# CRUD cycle
# ---------------------------------------------------------------------------

async def test_list_skills_empty(authed_client: AsyncClient):
    response = await authed_client.get("/api/skills/")
    assert response.status_code == 200
    assert response.json() == []


async def test_create_skill_with_explicit_fields(authed_client: AsyncClient):
    body = await _create_skill(authed_client)
    assert body["name"] == "test-skill"
    assert body["description"] == "A skill for testing."
    assert body["visibility"] == "private"
    assert body["is_owner"] is True
    assert body["id"] is not None


async def test_create_skill_from_frontmatter(authed_client: AsyncClient):
    """name/description come from the SKILL.md front matter; the stored
    content is the body with the front matter stripped."""
    resp = await authed_client.post("/api/skills/", json={"content": SKILL_MD})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "brand-voice"
    assert body["description"] == "How to write in the company voice."
    assert body["content"].startswith("# Brand voice")
    assert "---" not in body["content"]
    assert body["frontmatter"]["name"] == "brand-voice"


async def test_explicit_fields_beat_frontmatter(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "override-name", "content": SKILL_MD},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["name"] == "override-name"
    # description still resolved from the front matter
    assert resp.json()["description"] == "How to write in the company voice."


async def test_create_skill_requires_name(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"description": "d", "content": "# body"},
    )
    assert resp.status_code == 400
    assert "name" in resp.json()["detail"].lower()


async def test_create_skill_requires_description(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "no-description", "content": "# body"},
    )
    assert resp.status_code == 400
    assert "description" in resp.json()["detail"].lower()


async def test_create_skill_rejects_bad_name(authed_client: AsyncClient):
    for bad in ("Has Spaces", "CamelCase", "trailing-", "-leading", "double--hyphen"):
        resp = await authed_client.post(
            "/api/skills/",
            json={"name": bad, "description": "d", "content": "# body"},
        )
        assert resp.status_code == 400, f"{bad!r} should be rejected"


async def test_create_skill_rejects_malformed_frontmatter(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "bad-yaml", "description": "d",
              "content": "---\nname: [unclosed\n---\nbody"},
    )
    assert resp.status_code == 400
    assert "YAML" in resp.json()["detail"]


async def test_create_skill_rejects_oversized_content(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "too-big", "description": "d", "content": "x" * (64 * 1024 + 1)},
    )
    assert resp.status_code == 400
    assert "KB" in resp.json()["detail"]


async def test_create_skill_duplicate_name_conflicts(authed_client: AsyncClient):
    await _create_skill(authed_client, name="dupe-name")
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "dupe-name", "description": "d", "content": "# body"},
    )
    assert resp.status_code == 409


async def test_create_skill_rejects_bad_visibility(authed_client: AsyncClient):
    resp = await authed_client.post(
        "/api/skills/",
        json={"name": "bad-vis", "description": "d", "content": "# body",
              "visibility": "public"},
    )
    assert resp.status_code == 400


async def test_get_skill(authed_client: AsyncClient):
    created = await _create_skill(authed_client, name="fetchable")
    resp = await authed_client.get(f"/api/skills/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "fetchable"


async def test_get_skill_not_found(authed_client: AsyncClient):
    resp = await authed_client.get("/api/skills/99999")
    assert resp.status_code == 404


async def test_update_skill(authed_client: AsyncClient):
    created = await _create_skill(authed_client, name="updatable")
    resp = await authed_client.put(
        f"/api/skills/{created['id']}",
        json={"description": "New description.", "visibility": "org"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["description"] == "New description."
    assert body["visibility"] == "org"
    assert body["name"] == "updatable"


async def test_update_skill_reparses_frontmatter(authed_client: AsyncClient):
    created = await _create_skill(authed_client, name="reparsed")
    resp = await authed_client.put(
        f"/api/skills/{created['id']}",
        json={"content": SKILL_MD},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Front matter renames the skill when the request didn't set name itself.
    assert body["name"] == "brand-voice"
    assert "---" not in body["content"]


async def test_update_skill_requires_a_field(authed_client: AsyncClient):
    created = await _create_skill(authed_client, name="no-op")
    resp = await authed_client.put(f"/api/skills/{created['id']}", json={})
    assert resp.status_code == 400


async def test_update_skill_rename_conflicts(authed_client: AsyncClient):
    await _create_skill(authed_client, name="taken")
    other = await _create_skill(authed_client, name="renamable")
    resp = await authed_client.put(
        f"/api/skills/{other['id']}", json={"name": "taken"},
    )
    assert resp.status_code == 409


async def test_delete_skill(authed_client: AsyncClient):
    created = await _create_skill(authed_client, name="deletable")
    resp = await authed_client.delete(f"/api/skills/{created['id']}")
    assert resp.status_code == 204
    assert (await authed_client.get(f"/api/skills/{created['id']}")).status_code == 404


async def test_skills_unauthenticated(client: AsyncClient):
    resp = await client.get("/api/skills/")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# agent attachment (data.skills)
# ---------------------------------------------------------------------------

async def test_create_agent_with_skill_ref(authed_client: AsyncClient):
    skill = await _create_skill(authed_client, name="attached")
    config = {
        **VALID_AGENT_CONFIG,
        "data": {**VALID_AGENT_CONFIG["data"], "skills": [{"skillId": skill["id"]}]},
    }
    resp = await authed_client.post(
        "/api/agents/", json={"name": "Skilled Agent", "config": config},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["config"]["data"]["skills"] == [{"skillId": skill["id"]}]


async def test_create_agent_with_unknown_skill_ref_rejected(authed_client: AsyncClient):
    config = {
        **VALID_AGENT_CONFIG,
        "data": {**VALID_AGENT_CONFIG["data"], "skills": [{"skillId": 424242}]},
    }
    resp = await authed_client.post(
        "/api/agents/", json={"name": "Bad Refs", "config": config},
    )
    assert resp.status_code == 400
    assert "424242" in resp.json()["detail"]


async def test_update_agent_with_unknown_skill_ref_rejected(authed_client: AsyncClient):
    create = await authed_client.post(
        "/api/agents/", json={"name": "To Update", "config": VALID_AGENT_CONFIG},
    )
    agent_id = create.json()["id"]
    config = {
        **VALID_AGENT_CONFIG,
        "data": {**VALID_AGENT_CONFIG["data"], "skills": [{"skillId": 424242}]},
    }
    resp = await authed_client.put(
        f"/api/agents/{agent_id}", json={"config": config},
    )
    assert resp.status_code == 400


async def test_agent_config_rejects_malformed_skills_shape(authed_client: AsyncClient):
    config = {
        **VALID_AGENT_CONFIG,
        "data": {**VALID_AGENT_CONFIG["data"], "skills": [{"id": 1}]},
    }
    resp = await authed_client.post(
        "/api/agents/", json={"name": "Malformed", "config": config},
    )
    assert resp.status_code == 400
