"""
List skills endpoint.

Own org, honouring visibility: everything you created plus what colleagues
marked 'org'. No grant arm yet — scoped_resources keeps the query shape ready
for the day AccessGrant learns the 'skill' resource type.
"""
from typing import List

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
from src.services.org_scope import SKILL, scoped_resources

from .models import SkillResponse

router = APIRouter()


@router.get("/", response_model=List[SkillResponse])
@limiter.limit("30/minute")
async def list_skills(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """List all skills the authenticated user can access."""
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    skills = (
        scoped_resources(db, Skill, org, SKILL, granted_ids=None)
        .order_by(Skill.updated_at.desc())
        .all()
    )

    return [
        SkillResponse(
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
        for skill in skills
    ]
