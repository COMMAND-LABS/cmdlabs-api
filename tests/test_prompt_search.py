"""Prompt search sits under /api/prompts, so the Prompts module gates it.

The Prompts page used to search through /api/similarity-search, whose prefix
belongs to knowledge_bases. The Free plan has Prompts but not knowledge
bases, so a Free user's search box on their own Prompts page got a 404.
"""
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.db.models import Organization
from src.routers.prompts import search as prompt_search
from tests.org_isolation import client_for, make_tenant


def _free_owner(db: Session):
    owner = make_tenant(db, slug="prompt-search-free", account_id=9811,
                        role="manager", is_owner=True)
    org = db.query(Organization).filter(Organization.id == owner.org_id).one()
    org.pinned_plan = plans.PLAN_FREE
    db.flush()
    return owner


async def test_a_free_plan_owner_can_search_their_prompts(db: Session, _override_db, monkeypatch):
    calls = []

    async def fake_search(query, namespace, account_id, request):
        calls.append((query.query, namespace, account_id))
        return {"success": True, "results": []}

    monkeypatch.setattr(prompt_search, "run_similarity_search", fake_search)
    owner = _free_owner(db)
    async with client_for(owner) as c:
        resp = await c.post("/api/prompts/search", json={"query": "customs broker email"})
        old = await c.post("/api/similarity-search/search", json={"query": "x"})

    assert resp.status_code == 200, resp.text
    # Always the prompts namespace, always narrowed to the caller's own vectors.
    assert calls == [("customs broker email", "prompts", owner.account_id)]
    assert old.status_code == 404, "the old route stays behind knowledge_bases"


async def test_prompt_search_ignores_a_requested_namespace(db: Session, _override_db, monkeypatch):
    """The route has no namespace parameter: it cannot be pointed at crm."""
    seen = []

    async def fake_search(query, namespace, account_id, request):
        seen.append(namespace)
        return {"success": True, "results": []}

    monkeypatch.setattr(prompt_search, "run_similarity_search", fake_search)
    owner = _free_owner(db)
    async with client_for(owner) as c:
        await c.post("/api/prompts/search?namespace=crm", json={"query": "x"})
    assert seen == ["prompts"]
