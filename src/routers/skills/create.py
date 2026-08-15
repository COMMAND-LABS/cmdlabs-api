"""
Create skill endpoint.

Accepts either explicit name/description fields, or a full SKILL.md whose
front matter carries them (explicit fields win). Content is stored with the
front matter stripped; the parsed mapping is kept on the row.
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
from src.services.skill_markdown import SkillMarkdownError, parse_skill_markdown

from .models import (
    CreateSkillRequest,
    SkillResponse,
    validate_skill_content,
    validate_skill_description,
    validate_skill_name,
    validate_skill_visibility,
)

router = APIRouter()


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=SkillResponse)
@limiter.limit("10/minute")
async def create_skill(
    request_body: CreateSkillRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Create a new skill in the caller's organization."""
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    content = validate_skill_content(request_body.content)
    try:
        frontmatter, body = parse_skill_markdown(content)
    except SkillMarkdownError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    # Stripping the front matter must not leave an empty skill behind.
    body = validate_skill_content(body)

    name = request_body.name or frontmatter.get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill name is required (as a field, or as 'name:' in the front matter).",
        )
    name = validate_skill_name(name)

    description = request_body.description or frontmatter.get("description")
    if not description or not isinstance(description, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Skill description is required (as a field, or as 'description:' in the front matter).",
        )
    description = validate_skill_description(description)

    visibility = validate_skill_visibility(request_body.visibility or "private")

    # Friendlier than letting uq_skill_org_name surface as a bare 409 — but
    # the constraint stays the backstop for concurrent creates.
    existing = db.query(Skill.id).filter(
        Skill.org_id == org.org_id, Skill.name == name
    ).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A skill named '{name}' already exists in this organization.",
        )

    skill = Skill(
        org_id=org.org_id,
        account_id=account_id,
        name=name,
        description=description,
        content=body,
        frontmatter=frontmatter or None,
        visibility=visibility,
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
