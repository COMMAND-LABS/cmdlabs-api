"""
Get prompt endpoint.
"""
from fastapi import APIRouter, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account

from ._shared import get_owned_prompt_or_404
from .models import PromptResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/{prompt_id}", response_model=PromptResponse)
@limiter.limit("60/minute")
async def get_prompt(
    prompt_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Get a specific prompt by ID.
    
    Only returns prompts that belong to the authenticated user.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)
        
    prompt = get_owned_prompt_or_404(db, prompt_id, account_id)
        
    return prompt
