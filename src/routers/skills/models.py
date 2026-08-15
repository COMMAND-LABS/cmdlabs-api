"""
Shared Pydantic models and validation for the skills router.

Field resolution on create: an explicit request field wins, then the parsed
SKILL.md front matter, then (for name/description, which have no safe
default) a 400. Content is required and is stored with front matter stripped;
the parsed mapping is kept on the row for round-tripping.
"""
import re
from datetime import datetime
from typing import Any, Optional

from fastapi import HTTPException, status
from pydantic import BaseModel, ConfigDict

# The handle the model passes to load_skill. Kebab-case keeps it unambiguous
# in prose and identical to Anthropic's SKILL.md naming convention.
SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_NAME_MAX_LENGTH = 64
SKILL_DESCRIPTION_MAX_LENGTH = 1024
# Bodies ride into the model as a tool result, so the cap is a context-size
# guard as much as a storage one.
SKILL_CONTENT_MAX_BYTES = 64 * 1024

SKILL_VISIBILITIES = ("private", "org")


class CreateSkillRequest(BaseModel):
    """name/description may come from the content's front matter instead."""
    name: Optional[str] = None
    description: Optional[str] = None
    content: str
    visibility: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class UpdateSkillRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    content: Optional[str] = None
    visibility: Optional[str] = None

    model_config = ConfigDict(populate_by_name=True)


class SkillResponse(BaseModel):
    id: int
    name: str
    description: str
    content: str
    visibility: str
    frontmatter: Optional[dict[str, Any]] = None
    is_owner: Optional[bool] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


def validate_skill_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise _bad_request("Skill name cannot be empty")
    if len(name) > SKILL_NAME_MAX_LENGTH:
        raise _bad_request(f"Skill name must be at most {SKILL_NAME_MAX_LENGTH} characters")
    if not SKILL_NAME_PATTERN.match(name):
        raise _bad_request(
            "Skill name must be kebab-case: lowercase letters and digits "
            "separated by single hyphens (e.g. 'brand-voice')."
        )
    return name


def validate_skill_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise _bad_request(
            "Skill description cannot be empty — it is what the agent reads "
            "when deciding whether to load this skill."
        )
    if len(description) > SKILL_DESCRIPTION_MAX_LENGTH:
        raise _bad_request(
            f"Skill description must be at most {SKILL_DESCRIPTION_MAX_LENGTH} characters"
        )
    return description


def validate_skill_content(content: str) -> str:
    if not content or not content.strip():
        raise _bad_request("Skill content cannot be empty")
    if len(content.encode("utf-8")) > SKILL_CONTENT_MAX_BYTES:
        raise _bad_request(
            f"Skill content must be at most {SKILL_CONTENT_MAX_BYTES // 1024} KB"
        )
    return content


def validate_skill_visibility(visibility: str) -> str:
    if visibility not in SKILL_VISIBILITIES:
        raise _bad_request("Skill visibility must be 'private' or 'org'")
    return visibility
