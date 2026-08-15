"""
Get skill endpoint. 404 for absent and for other-tenant alike, so an id never
confirms it belongs to somebody else.
"""
from fastapi import APIRouter, Request

from src.db.models import Skill
from src.deps import (
    account_id_from_claims,
    db_dependency,
    ensure_account,
    jwt_dependency,
    org_dependency,
)
from src.rate_limit import limiter
from src.services.org_scope import get_resource_or_404

from .models import SkillResponse

router = APIRouter()


@router.get("/{skill_id}", response_model=SkillResponse)
@limiter.limit("30/minute")
async def get_skill(
    skill_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Get a specific skill by ID."""
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    skill = get_resource_or_404(db, Skill, skill_id, org)

    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        visibility=skill.visibility,
        frontmatter=skill.frontmatter,
        is_owner=(skill.account_id == account_id),
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )
