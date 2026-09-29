import logging
from fastapi import APIRouter, Request
from pydantic import BaseModel
import os
from src.core.clients import pc
from src.deps import jwt_dependency, account_id_from_claims
from src.services import fetch_embedding
from src.rate_limit import limiter
from ._shared import SEARCHABLE_NAMESPACES, owner_filter

logger = logging.getLogger(__name__)

router = APIRouter()

class Query(BaseModel):
    query: str
    top_k: int = 5
    similarity_threshold: float = 0.0

@router.post("/search")
@limiter.limit("50/minute")
async def similarity_search(
    query: Query,
    namespace: str = "prompts",
    decoded_jwt: jwt_dependency = None,
    request: Request = None
):
    """
    Perform similarity search over the caller's OWN vectors in `namespace`.

    Only the namespaces in SEARCHABLE_NAMESPACES may be searched, and results
    are filtered to vectors the caller owns (see _shared.py) — a namespace is
    shared by every account, so it is never an access boundary on its own.

    Gated by the knowledge_bases module (its path prefix). The Prompts page now
    searches through POST /api/prompts/search instead, gated by `prompts`, so
    plans with Prompts but no knowledge bases (Free) can search their prompts.
    """
    return await run_similarity_search(
        query, namespace, account_id_from_claims(decoded_jwt), request)


async def run_similarity_search(
    query: Query, namespace: str, account_id: int, request: Request | None,
) -> dict:
    """The search itself, shared by this route and /api/prompts/search."""
    filter_ = owner_filter(namespace, account_id)
    if filter_ is None:
        return {
            "success": False,
            "error": f"Unsupported namespace. Use one of: {', '.join(SEARCHABLE_NAMESPACES)}.",
        }

    try:
        # Extract JWT from cookie or Authorization header
        token = None
        if request:
            token = request.cookies.get("jwt")
            if not token:
                auth_header = request.headers.get("Authorization", "")
                if auth_header.startswith("Bearer "):
                    token = auth_header.removeprefix("Bearer ").strip()

        # Get embedding for the query
        embedding = await fetch_embedding(token, query.query)
        
        if embedding is None:
            return {
                "success": False,
                "error": "Failed to generate embedding for query"
            }
        
        # Get the index name from environment variables
        index_name = os.getenv("PINECONE_ALL_MINILM_L6_V2_INDEX")
        
        # Get Pinecone index
        index = pc.Index(index_name)
        
        # Perform similarity search
        results = index.query(
            vector=embedding,
            top_k=query.top_k,
            include_values=False,
            include_metadata=True,
            namespace=namespace,
            filter=filter_,
        )
        
        # Filter results by similarity threshold if provided
        if query.similarity_threshold > 0.0:
            filtered_matches = [
                r for r in results['matches'] 
                if r['score'] >= query.similarity_threshold
            ]
        else:
            filtered_matches = results['matches']
        
        final_results = [{'metadata': r['metadata'], 'score': r['score']} for r in filtered_matches]

        return {
            "success": True,
            "query": query.query,
            "top_k": query.top_k,
            "similarity_threshold": query.similarity_threshold,
            "namespace": namespace,
            "results": final_results
        }
        
    except Exception as e:
        logger.error("[SIMILARITY SEARCH] %s: %s", type(e).__name__, e)
        return {
            "success": False,
            "error": "An unexpected error occurred. Please try again.",
        }