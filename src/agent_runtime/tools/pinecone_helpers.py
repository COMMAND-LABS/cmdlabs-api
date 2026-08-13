"""Shared helpers for Pinecone-based vector search tools.

Consolidates credential loading, embedding generation, and Pinecone querying
so that ``vector_search`` and ``vector_search_with_reranking`` avoid
duplicating ~100 lines of identical setup and query code.
"""

import logging
import os
from typing import Any

import aiohttp
from sqlalchemy.orm import Session

from src.routers.credentials.encryption import get_credential_value
from src.services.vector_store_credentials import resolve_index_pinecone_credential

logger = logging.getLogger(__name__)


def load_pinecone_index(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    **kwargs,
):
    """Validate config, load Pinecone credentials, and return (index, namespace, index_name).

    Returns ``None`` if setup fails (missing config, bad credentials, etc.).
    """
    provider = tool_config.get("provider", "").lower()
    index_name = tool_config.get("index")
    namespace = tool_config.get("namespace")

    if not all([provider, index_name, namespace]):
        logger.warning(f"[VECTOR SEARCH] Missing required fields: provider={provider}, index={index_name}, namespace={namespace}")
        return None

    if provider != "pinecone":
        logger.warning(f"[VECTOR SEARCH] Unsupported provider: {provider}")
        return None

    credential_account_id = kwargs.get("agent_owner_account_id", account_id)

    # Honor the knowledge base's explicit Pinecone credential binding (falls back
    # to the owner's default). Index-scoped so different KBs can use different keys.
    credential = resolve_index_pinecone_credential(db, credential_account_id, index_name)

    if not credential:
        logger.warning(f"[VECTOR SEARCH] No Pinecone API key found for account {credential_account_id}")
        return None

    try:
        pinecone_api_key = get_credential_value(credential, "api_key")
    except Exception as exc:
        logger.error(f"[VECTOR SEARCH] Failed to decrypt Pinecone API key: {exc}")
        return None

    from pinecone import Pinecone

    pc_client = Pinecone(api_key=pinecone_api_key)
    index = pc_client.Index(index_name)
    return index, namespace, index_name


async def generate_embedding(query: str, auth_token: str | None = None) -> list[float] | None:
    """Call the embeddings microservice and return the embedding vector, or ``None`` on failure."""
    headers = {}
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"

    print("EMBEDDINGS_API_URL")
    print(f"{os.getenv('EMBEDDINGS_API_URL')}")

    url = f"{os.getenv('EMBEDDINGS_API_URL')}/huggingface/embedding"

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json={"input": query}, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"[VECTOR SEARCH] Embeddings API error ({response.status}): {error_text}")
                    return None
                result = await response.json()
                return result["embedding"]
        except aiohttp.ClientError as exc:
            logger.error(f"[VECTOR SEARCH] Error generating embedding: {exc}")
            return None


async def query_pinecone(
    index,
    embedding: list[float],
    namespace: str,
    top_k: int,
) -> list[dict[str, Any]] | None:
    """Run a Pinecone similarity query and return the matches list."""
    results = index.query(
        vector=embedding,
        top_k=top_k,
        include_values=False,
        include_metadata=True,
        namespace=namespace,
    )
    return results.get("matches", [])


def format_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format Pinecone matches into the standard tool output shape."""
    return [
        {
            "metadata": m.get("metadata", {}),
            "score": m.get("score", 0.0),
            "id": m.get("id"),
        }
        for m in matches
    ]
