"""
Delete prompt endpoint.

Also removes the corresponding vector from the ``prompts`` namespace in
Pinecone so search results stay in sync.
"""
import logging
from fastapi import APIRouter, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.core.clients import pc
from src.rate_limit import limiter

from ._shared import PINECONE_INDEX, PROMPTS_NAMESPACE, get_owned_prompt_or_404

logger = logging.getLogger(__name__)

router = APIRouter()

@router.delete("/{prompt_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_prompt(
    prompt_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Delete a prompt and its corresponding Pinecone vector.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)
        
    prompt = get_owned_prompt_or_404(db, prompt_id, account_id)
        
    db.delete(prompt)
    db.commit()

    # ── Remove vector from Pinecone ──────────────────────────────
    try:
        if PINECONE_INDEX:
            index = pc.Index(PINECONE_INDEX)
            index.delete(ids=[f"prompt_{prompt_id}"], namespace=PROMPTS_NAMESPACE)
            logger.info("[DELETE PROMPT] Removed vector prompt_%s from Pinecone", prompt_id)
    except Exception as vec_err:
        logger.warning("[DELETE PROMPT] Failed to delete vector: %s", vec_err)
        
    return None
