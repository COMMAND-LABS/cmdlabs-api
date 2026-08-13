"""Vector Search Tool — semantic search over Pinecone, with optional reranking.

A single builder backs two registered tool types (see ``tools/__init__.py``):
- ``vectorSearch`` (``reranking=False``): one-stage semantic search.
- ``vectorSearchWithReranking`` (``reranking=True``): two-stage retrieve-then-
  rerank, which degrades to one-stage search when ``RERANKER_API_URL`` is unset
  or the reranker is unavailable.
"""

import logging
import os
from typing import Any

import aiohttp
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.agent_runtime.tools.pinecone_helpers import (
    format_matches,
    generate_embedding,
    load_pinecone_index,
    query_pinecone,
)

logger = logging.getLogger(__name__)


async def create_vector_search_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    *,
    reranking: bool = False,
    **kwargs,
) -> StructuredTool | None:
    setup = load_pinecone_index(tool_config, account_id, db, **kwargs)
    if not setup:
        return None
    index, namespace, index_name = setup

    if reranking:
        default_description = f"Search and rerank the {namespace} knowledge base"
        default_name = "vector_search_with_reranking"
    else:
        default_description = f"Search the {namespace} knowledge base"
        default_name = "vector_search"

    description = tool_config.get("description", default_description)
    top_k_default = tool_config.get("topK", 20 if reranking else 10)
    top_n_default = tool_config.get("topN", 5)

    async def retrieval_impl(query: str, top_k: int = top_k_default, top_n: int = top_n_default) -> dict:
        """Retrieve (and optionally rerank) relevant documents from the knowledge base."""
        try:
            embedding = await generate_embedding(query, auth_token)
            if embedding is None:
                return {"error": "Failed to generate embedding"}

            matches = await query_pinecone(index, embedding, namespace, top_k)
            if not matches:
                empty = {"results": [], "message": "No relevant documents found"}
                if reranking:
                    empty |= {"namespace": namespace, "index": index_name}
                return empty

            if not reranking:
                return {
                    "results": format_matches(matches),
                    "namespace": namespace,
                    "index": index_name,
                }

            return await _rerank_and_format(
                query, matches, top_n, namespace, index_name, auth_token
            )
        except Exception as exc:
            logger.error(f"[VECTOR SEARCH] Error: {exc}")
            return {"error": str(exc)}

    if reranking:
        class SearchQuery(BaseModel):
            query: str = Field(description="The search query to find relevant documents")
            top_k: int = Field(
                default=top_k_default,
                description=f"Number of initial candidates to retrieve (default: {top_k_default})",
            )
            top_n: int = Field(
                default=top_n_default,
                description=f"Number of final reranked results to return (default: {top_n_default})",
            )
    else:
        class SearchQuery(BaseModel):
            query: str = Field(description="The search query to find relevant documents")
            top_k: int = Field(
                default=top_k_default,
                description=f"Number of results to return (default: {top_k_default})",
            )

    return StructuredTool(
        func=retrieval_impl,
        coroutine=retrieval_impl,
        name=tool_config.get("name", default_name),
        description=description,
        args_schema=SearchQuery,
    )


async def _rerank_and_format(
    query: str,
    matches: list,
    top_n: int,
    namespace: str,
    index_name: str,
    auth_token: str | None,
) -> dict:
    """Second-stage rerank of *matches*; degrades to top-N similarity on failure."""
    docs = [m.get("metadata", {}).get("content", "") or "No content available" for m in matches]
    similarity_scores = [m.get("score", 0.0) for m in matches]

    reranker_api_url = os.getenv("RERANKER_API_URL")
    if not reranker_api_url:
        return {
            "results": format_matches(matches[:top_n]),
            "namespace": namespace,
            "index": index_name,
            "reranking_applied": False,
        }

    reranked = await _call_reranker(reranker_api_url, query, docs, auth_token)
    if reranked is None:
        return {
            "results": format_matches(matches[:top_n]),
            "namespace": namespace,
            "index": index_name,
            "reranking_applied": False,
        }

    formatted = []
    for item in reranked[:top_n]:
        idx = item.get("index")
        if idx is not None and idx < len(matches):
            entry = format_matches([matches[idx]])[0]
            entry["score"] = item.get("relevance_score", 0.0)
            entry["similarity_score"] = similarity_scores[idx]
            formatted.append(entry)

    return {
        "results": formatted,
        "namespace": namespace,
        "index": index_name,
        "reranking_applied": True,
        "initial_candidates": len(docs),
        "final_results": len(formatted),
    }


async def _call_reranker(base_url: str, query: str, documents: list, auth_token: str | None) -> list | None:
    """Call the reranker microservice. Returns the ranked results list or None on failure."""
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    endpoint = f"{base_url.rstrip('/')}/huggingface/rerank"
    try:
        async with (
            aiohttp.ClientSession() as session,
            session.post(endpoint, json={"query": query, "documents": documents}, headers=headers) as resp,
        ):
            if resp.status != 200:
                logger.error(f"[VECTOR SEARCH] Reranker API error ({resp.status})")
                return None
            result = await resp.json()
            return result.get("results", [])
    except Exception as exc:
        logger.error(f"[VECTOR SEARCH] Reranker call failed: {exc}")
        return None
