"""/api/similarity-search is scoped to the caller's own vectors.

The Pinecone index and its namespaces are shared by every account, so a
namespace is never an access boundary: `crm` holds every account's contact
history. These pin that search refuses other namespaces and always carries the
caller's owner filter, and that delete removes only the caller's uploads.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from httpx import AsyncClient

from src.db.models import Account

SEARCH = "/api/similarity-search/search"
DELETE = "/api/similarity-search/delete-vectors"


def _index_with(vectors: dict) -> MagicMock:
    """A fake Pinecone index holding {id: metadata} in one page."""
    index = MagicMock()
    index.list_paginated.return_value = SimpleNamespace(
        vectors=[SimpleNamespace(id=i) for i in vectors], pagination=None)
    index.fetch.side_effect = lambda ids, namespace: SimpleNamespace(
        vectors={i: SimpleNamespace(metadata=vectors[i]) for i in ids})
    index.query.return_value = {"matches": []}
    return index


async def test_search_refuses_the_crm_namespace(authed_client: AsyncClient, test_account: Account):
    index = _index_with({})
    with patch("src.routers.similaritySearch.search.pc.Index", return_value=index), \
         patch("src.routers.similaritySearch.search.fetch_embedding", return_value=[0.1]):
        response = await authed_client.post(f"{SEARCH}?namespace=crm", json={"query": "q"})

    assert response.status_code == 200
    assert response.json()["success"] is False
    index.query.assert_not_called()


async def test_search_is_filtered_to_the_callers_vectors(authed_client: AsyncClient, test_account: Account):
    index = _index_with({})
    with patch("src.routers.similaritySearch.search.pc.Index", return_value=index), \
         patch("src.routers.similaritySearch.search.fetch_embedding", return_value=[0.1]):
        prompts = await authed_client.post(f"{SEARCH}?namespace=prompts", json={"query": "q"})
        uploads = await authed_client.post(f"{SEARCH}?namespace=similarity_search", json={"query": "q"})

    assert prompts.json()["success"] is True and uploads.json()["success"] is True
    filters = [c.kwargs["filter"] for c in index.query.call_args_list]
    assert filters == [
        {"account_id": {"$eq": test_account.id}},
        {"user_id": {"$eq": str(test_account.id)}},
    ]


async def test_delete_removes_only_the_callers_uploads(authed_client: AsyncClient, test_account: Account):
    index = _index_with({
        "mine-1": {"user_id": str(test_account.id)},
        "theirs": {"user_id": "999"},
        "mine-2": {"user_id": str(test_account.id)},
        "no-owner": {},
    })
    with patch("src.routers.similaritySearch.delete_vectors.pc.Index", return_value=index):
        response = await authed_client.request("DELETE", DELETE, json={"namespace": "anything"})

    body = response.json()
    assert body["success"] is True and body["deleted_count"] == 2
    deleted = [i for c in index.delete.call_args_list for i in c.kwargs["ids"]]
    assert sorted(deleted) == ["mine-1", "mine-2"]
    assert all("delete_all" not in c.kwargs for c in index.delete.call_args_list), \
        "the shared namespace must never be wiped"
