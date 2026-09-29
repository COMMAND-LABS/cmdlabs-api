"""
Search the caller's prompts by meaning.

The same search as POST /api/similarity-search/search with namespace=prompts,
mounted under /api/prompts so it is gated by the `prompts` module. The
similarity-search prefix belongs to `knowledge_bases`, which the Free plan
does not include, so Free users — who do have Prompts — got a 404 from the
search box on their own Prompts page. The old route stays for other callers.
"""
from fastapi import APIRouter, Request

from src.deps import account_id_from_claims, jwt_dependency
from src.rate_limit import limiter
from src.routers.similaritySearch.search import Query, run_similarity_search

router = APIRouter()


@router.post("/search")
@limiter.limit("50/minute")
async def search_prompts(query: Query, decoded_jwt: jwt_dependency, request: Request):
    return await run_similarity_search(
        query, "prompts", account_id_from_claims(decoded_jwt), request)
