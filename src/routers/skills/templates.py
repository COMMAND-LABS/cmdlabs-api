"""
Built-in skill templates: list them, install one as an org-owned skill.

Installing COPIES the template into a normal `skills` row owned by the
caller (private, like any create) — from then on it is the org's skill to
edit, and the template file is out of the picture. The 409 on a name clash
is the same one /api/skills/ create returns: an org that already has a
'skill-creator' (hand-written or previously installed) keeps theirs.

Mounted before the /{skill_id} routes so 'templates' is never read as an id.
"""
from typing import List

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from src.db.models import Skill
from src.deps import (
    account_id_from_claims,
    db_dependency,
    ensure_account,
    jwt_dependency,
    org_dependency,
)
from src.rate_limit import limiter
from src.services.skill_templates import get_skill_template, load_skill_templates

from .models import SkillResponse

router = APIRouter()


class SkillTemplateResponse(BaseModel):
    name: str
    description: str
    content: str
    # True when a skill with this name already exists in the caller's org
    # (installed or hand-written) — the UI uses it to hide the install CTA.
    installed: bool


@router.get("/templates/", response_model=List[SkillTemplateResponse])
@limiter.limit("30/minute")
async def list_skill_templates(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Built-in templates an org can install as its own skills."""
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    templates = load_skill_templates()
    existing_names = {
        name for (name,) in db.query(Skill.name).filter(
            Skill.org_id == org.org_id, Skill.name.in_(list(templates)),
        ).all()
    }
    return [
        SkillTemplateResponse(
            name=t.name,
            description=t.description,
            content=t.content,
            installed=t.name in existing_names,
        )
        for t in templates.values()
    ]


@router.post(
    "/templates/{template_name}/install",
    status_code=status.HTTP_201_CREATED,
    response_model=SkillResponse,
)
@limiter.limit("10/minute")
async def install_skill_template(
    template_name: str,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Copy a built-in template into the caller's org as a private skill."""
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    template = get_skill_template(template_name)
    if template is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No built-in skill template named '{template_name}'.",
        )

    existing = db.query(Skill.id).filter(
        Skill.org_id == org.org_id, Skill.name == template.name
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A skill named '{template.name}' already exists in this organization.",
        )

    skill = Skill(
        org_id=org.org_id,
        account_id=account_id,
        name=template.name,
        description=template.description,
        content=template.content,
        frontmatter=template.frontmatter or None,
        visibility="private",
    )
    db.add(skill)
    db.commit()
    db.refresh(skill)

    return SkillResponse(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        content=skill.content,
        visibility=skill.visibility,
        frontmatter=skill.frontmatter,
        is_owner=True,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )
