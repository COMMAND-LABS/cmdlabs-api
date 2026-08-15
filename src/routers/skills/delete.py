"""
Delete skill endpoint.

Agents referencing the deleted skill are NOT rewritten: the reference lives
inside the config JSON blob, and the runtime already fail-softs a skill id it
cannot resolve (agent_runtime/skills.py logs and skips). Editing every
referencing config here would mean scanning JSON for a cascade the runtime
does not need.
"""
from fastapi import APIRouter, Request, status

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

router = APIRouter()


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def delete_skill(
    skill_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Delete a skill by ID."""
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    skill = get_resource_or_404(db, Skill, skill_id, org)

    db.delete(skill)
    db.commit()

    return None
