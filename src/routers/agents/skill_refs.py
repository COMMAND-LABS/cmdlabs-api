"""
Skill reference validation for agent configs.

The JSON schema validates the SHAPE of data.skills; this validates the
REFERENCES — each skillId must resolve to a skill the caller can see in
their org (own, or org-visible). Rejecting at write time names the bad id
for the person who can fix it; the runtime's fail-soft skip (agent_runtime/
skills.py) is the backstop for rows deleted after attachment, not the gate.
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.db.models import Skill
from src.services.org_scope import resource_predicate


def validate_skill_refs(db: Session, config: dict, org) -> None:
    """Raise 400 if any data.skills entry references an unreachable skill.

    Runs after schema validation, so entries are known to be
    {"skillId": int} dicts.
    """
    skill_refs = (config.get("data") or {}).get("skills") or []
    skill_ids = {ref["skillId"] for ref in skill_refs if isinstance(ref, dict)}
    if not skill_ids:
        return

    visible_ids = {
        row.id
        for row in db.query(Skill.id).filter(
            Skill.id.in_(skill_ids),
            resource_predicate(Skill, org),
        )
    }
    missing = sorted(skill_ids - visible_ids)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Agent config references skill id(s) that do not exist or "
                f"are not accessible: {missing}"
            ),
        )
