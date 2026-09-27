"""
Update prompt endpoint.

After updating the DB row, re-embeds the content and upserts the vector
into the ``prompts`` namespace in Pinecone so search results stay fresh.
"""
import logging
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account

from ._shared import get_owned_prompt_or_404, upsert_prompt_vector
from .models import UpdatePromptRequest, PromptResponse
from src.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

@router.put("/{prompt_id}", response_model=PromptResponse)
@limiter.limit("30/minute")
async def update_prompt(
    prompt_id: int,
    request_body: UpdatePromptRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Update an existing prompt.

    If content, name, or description changed the vector in Pinecone is
    re-embedded and upserted so search stays in sync.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)
        
    prompt = get_owned_prompt_or_404(db, prompt_id, account_id)
        
    needs_reembed = False

    if request_body.name is not None:
        if not request_body.name.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prompt name cannot be empty"
            )
        prompt.name = request_body.name.strip()
        needs_reembed = True
        
    if request_body.description is not None:
        prompt.description = request_body.description.strip() if request_body.description else None
        needs_reembed = True
        
    if request_body.content is not None:
        if not request_body.content.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Prompt content cannot be empty"
            )
        prompt.content = request_body.content
        needs_reembed = True
        
    db.commit()
    db.refresh(prompt)

    # ── Re-embed + upsert to Pinecone ────────────────────────────
    if needs_reembed:
        try:
            if await upsert_prompt_vector(request, prompt, account_id):
                logger.info("[UPDATE PROMPT] Re-embedded prompt %s into Pinecone", prompt.id)
        except Exception as embed_err:
            logger.warning("[UPDATE PROMPT] Re-embedding failed: %s", embed_err)
        
    return prompt
