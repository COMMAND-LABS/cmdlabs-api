"""
Update skill endpoint.

Same write gate as agents: get_resource_or_404, so any member who can SEE the
skill (creator, or colleague of an 'org'-visible one) may edit it — matching
how org-visible agents already behave.

Updated content is re-parsed for front matter, and front-matter
name/description apply only when the request did not set the field explicitly
(same precedence as create).
"""
from fastapi import APIRouter, HTTPException, Request, status

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
from src.services.skill_markdown import SkillMarkdownError, parse_skill_markdown

from .models import (
    SkillResponse,
    UpdateSkillRequest,
    validate_skill_content,
    validate_skill_description,
    validate_skill_name,
    validate_skill_visibility,
)

router = APIRouter()


@router.put("/{skill_id}", response_model=SkillResponse)
@limiter.limit("10/minute")
async def update_skill(
    skill_id: int,
    request_body: UpdateSkillRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Update an existing skill."""
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    skill = get_resource_or_404(db, Skill, skill_id, org)

    if all(
        getattr(request_body, field) is None
        for field in ("name", "description", "content", "visibility")
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field (name, description, content, visibility) must be provided for update",
        )

    new_name = request_body.name
    new_description = request_body.description

    if request_body.content is not None:
        content = validate_skill_content(request_body.content)
        try:
            frontmatter, body = parse_skill_markdown(content)
        except SkillMarkdownError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
        body = validate_skill_content(body)
        skill.content = body
        skill.frontmatter = frontmatter or None
        if new_name is None and isinstance(frontmatter.get("name"), str):
            new_name = frontmatter["name"]
        if new_description is None and isinstance(frontmatter.get("description"), str):
            new_description = frontmatter["description"]

    if new_name is not None:
        new_name = validate_skill_name(new_name)
        if new_name != skill.name:
            duplicate = db.query(Skill.id).filter(
                Skill.org_id == org.org_id,
                Skill.name == new_name,
                Skill.id != skill.id,
            ).first()
            if duplicate:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"A skill named '{new_name}' already exists in this organization.",
                )
            skill.name = new_name

    if new_description is not None:
        skill.description = validate_skill_description(new_description)

    if request_body.visibility is not None:
        skill.visibility = validate_skill_visibility(request_body.visibility)

    db.commit()
    db.refresh(skill)

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
