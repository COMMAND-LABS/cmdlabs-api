"""
Skills: org-scoped and visibility-aware, mirroring test_org_isolation_resources.

Also pins the cross-org ATTACHMENT boundary: an agent config may not reference
another tenant's skill, even by a guessed id — the write-time validator uses
the same resource predicate as the reads, so a foreign id is indistinguishable
from an absent one.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import Skill
from src.rate_limit import limiter
from tests.org_isolation import client_for, make_tenant

SKILLS_URL = "/api/skills/"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Shared in-memory rate budget — see test_skills.py."""
    limiter.reset()
    yield


def _skill(t, name, visibility="private"):
    return Skill(org_id=t.org_id, account_id=t.account_id, name=name,
                 visibility=visibility, description=f"About {name}.",
                 content=f"# {name}\n\nInstructions.")


async def _visible_ids(tenant):
    async with client_for(tenant) as c:
        resp = await c.get(SKILLS_URL)
    assert resp.status_code == 200, resp.text
    return {s["id"] for s in resp.json()}


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="acme-skill", account_id=5401, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="beta-skill", account_id=5402, data_scope="shared")


# ---------------------------------------------------------------------------
# the org boundary
# ---------------------------------------------------------------------------

async def test_skills_do_not_cross_orgs(db: Session, _override_db, acme, beta):
    mine = _skill(acme, "acme-skill", visibility="org")
    db.add(mine); db.flush()
    assert mine.id not in await _visible_ids(beta)


async def test_single_skill_read_is_org_confined(db: Session, _override_db, acme, beta):
    """The list predicate and its single-row twin must agree — a row absent
    from the list but readable by id is the classic silent leak."""
    mine = _skill(acme, "acme-only", visibility="org")
    db.add(mine); db.flush()
    async with client_for(beta) as c:
        resp = await c.get(f"{SKILLS_URL}{mine.id}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# visibility, inside one org
# ---------------------------------------------------------------------------

async def test_private_skill_is_hidden_from_colleagues(db: Session, _override_db, acme):
    colleague = make_tenant(db, slug="acme-skill", account_id=5403, data_scope="shared")
    private = _skill(acme, "work-in-progress", visibility="private")
    db.add(private); db.flush()

    assert private.id in await _visible_ids(acme), "creator must see their own"
    assert private.id not in await _visible_ids(colleague)


async def test_org_visible_skill_is_shared_with_colleagues(db: Session, _override_db, acme):
    colleague = make_tenant(db, slug="acme-skill", account_id=5404, data_scope="shared")
    shared = _skill(acme, "team-skill", visibility="org")
    db.add(shared); db.flush()

    assert shared.id in await _visible_ids(colleague)


# ---------------------------------------------------------------------------
# the attachment boundary
# ---------------------------------------------------------------------------

async def test_agent_cannot_reference_another_orgs_skill(
    db: Session, _override_db, acme, beta
):
    theirs = _skill(beta, "their-secret", visibility="org")
    db.add(theirs); db.flush()

    config = {
        "schema": "agent_config",
        "version": 4,
        "data": {
            "systemPrompt": "You are a test assistant.",
            "skills": [{"skillId": theirs.id}],
        },
    }
    async with client_for(acme) as c:
        resp = await c.post("/api/agents/", json={"name": "Reacher", "config": config})
    assert resp.status_code == 400
    assert str(theirs.id) in resp.json()["detail"]
