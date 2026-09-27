"""
Helpers shared by the prompt endpoints (one file per endpoint; the common
bits live here).
"""
import os
from fastapi import HTTPException, status, Request
from src.db.models import Prompt
from src.services import fetch_embedding
from src.services.crm_vector_service import extract_token
from src.core.clients import pc

PINECONE_INDEX = os.getenv("PINECONE_ALL_MINILM_L6_V2_INDEX")
PROMPTS_NAMESPACE = "prompts"


def get_owned_prompt_or_404(db, prompt_id: int, account_id: int) -> Prompt:
    """The caller's prompt with this id, or 404 "Prompt not found"."""
    prompt = db.query(Prompt).filter(
        Prompt.id == prompt_id,
        Prompt.account_id == account_id
    ).first()

    if not prompt:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Prompt not found"
        )

    return prompt


async def upsert_prompt_vector(request: Request, prompt: Prompt, account_id: int) -> bool:
    """Embed the prompt's content and upsert it into the ``prompts`` namespace.

    Returns True when a vector was upserted, False when there was no embedding
    or no index configured. Errors propagate: callers log them and carry on.
    """
    token = extract_token(request)
    embedding = await fetch_embedding(token, prompt.content)

    if embedding and PINECONE_INDEX:
        index = pc.Index(PINECONE_INDEX)
        index.upsert(
            vectors=[(
                f"prompt_{prompt.id}",
                embedding,
                {
                    "prompt_id": prompt.id,
                    "account_id": account_id,
                    "name": prompt.name,
                    "description": prompt.description or "",
                    "content": prompt.content,
                    "type": "prompt",
                },
            )],
            namespace=PROMPTS_NAMESPACE,
        )
        return True
    return False
