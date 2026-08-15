"""
Agent Skills at runtime: progressive disclosure.

An agent's config references skills by id (data.skills). At runtime the
system prompt carries only an INDEX — each skill's name and description —
and the full markdown body is fetched on demand through the load_skill tool
built here. That two-tier shape is the point of skills: an agent can carry
many without paying their combined length on every turn.

Failure direction, in both axes:

  - A skill id that no longer resolves (deleted, made private by a colleague,
    somehow cross-org) is LOGGED AND SKIPPED, mirroring the tool factory: a
    stale reference must degrade the agent, never kill it. Write-time
    validation (routers/agents/skill_refs.py) keeps this path rare.

  - Entitlement fails CLOSED. The 'skills' module is premium; when the
    CALLER's plan∩role does not include it, no index is injected and no tool
    is built — the same reasoning as tool_entitlement: require_module() gates
    /api/skills, and without this check the agent runtime would be the way
    around it.

Visibility is evaluated against the AGENT OWNER, not the caller: the owner
attached the skills, so a colleague running a shared agent gets the same
agent the owner built — exactly how the agent's tools already behave.

Bodies are loaded eagerly with the index (≤20 skills × ≤64 KB, one query) so
the tool needs no DB session at invoke time — tools outlive the request
session, which context.py closes before streaming.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_

from src.agent_runtime.tool_entitlement import effective_modules
from src.db.models import Skill
from src.services.org_scope import OrgScope

logger = logging.getLogger(__name__)

SKILLS_MODULE_KEY = "skills"

LOAD_SKILL_TOOL_NAME = "load_skill"


@dataclass(frozen=True)
class AttachedSkill:
    """The subset of a skill row the runtime needs, detached from the ORM so
    it stays valid after the request session closes."""
    name: str
    description: str
    content: str


def load_agent_skills(db, agent, org_scope: OrgScope) -> list[AttachedSkill]:
    """Resolve an agent's attached skills, entitlement- and visibility-checked.

    Returns them in config order — the order the owner arranged them in is
    the order the model reads the index.
    """
    if agent is None or not agent.config:
        return []
    skill_refs = (agent.config.get("data") or {}).get("skills") or []
    skill_ids = [ref["skillId"] for ref in skill_refs
                 if isinstance(ref, dict) and isinstance(ref.get("skillId"), int)]
    if not skill_ids:
        return []

    granted = effective_modules(db, org_scope.account_id, org_scope.org_id)
    if SKILLS_MODULE_KEY not in granted:
        logger.info(
            "[SKILLS] dropping %d skill(s) — %s not enabled for this caller",
            len(skill_ids), SKILLS_MODULE_KEY,
        )
        return []

    rows = db.query(Skill).filter(
        Skill.id.in_(skill_ids),
        and_(
            Skill.org_id == agent.org_id,
            or_(Skill.visibility == "org", Skill.account_id == agent.account_id),
        ),
    ).all()
    by_id = {row.id: row for row in rows}

    skills: list[AttachedSkill] = []
    seen: set[int] = set()
    for skill_id in skill_ids:
        if skill_id in seen:
            continue
        seen.add(skill_id)
        row = by_id.get(skill_id)
        if row is None:
            logger.warning(
                "[SKILLS] agent %s references skill %s which no longer resolves "
                "— skipping", getattr(agent, "id", None), skill_id,
            )
            continue
        skills.append(AttachedSkill(
            name=row.name,
            description=row.description,
            content=row.content,
        ))
    return skills


def _escape_braces(text: str) -> str:
    """The same {→{{ escaping context.py applies to the base system prompt.

    Skill names and descriptions are user-authored; a literal brace in either
    would otherwise be parsed as a variable by LangChain's f-string template
    engine and crash the turn.
    """
    return text.replace("{", "{{").replace("}", "}}")


def build_skills_guidance(skills: list[AttachedSkill]) -> str:
    """The system-prompt index block, ready to append (already brace-escaped).

    Follows the THINK_SYSTEM_GUIDANCE precedent: the runtime's second prompt
    contributor after the stored systemPrompt.
    """
    if not skills:
        return ""
    lines = [
        "\n\n<available_skills>",
        "You have skills: packages of instructions for specific tasks. "
        "When a request matches a skill's description, call the "
        f"{LOAD_SKILL_TOOL_NAME} tool with that skill's name FIRST, then "
        "follow the loaded instructions. Do not guess at what a skill "
        "contains — load it.",
    ]
    for skill in skills:
        lines.append(f"- {skill.name}: {skill.description}")
    lines.append("</available_skills>")
    return _escape_braces("\n".join(lines))


class LoadSkillInput(BaseModel):
    skill_name: str = Field(
        description="Name of the skill to load, exactly as listed in "
                    "<available_skills>.",
    )


def create_load_skill_tool(skills: list[AttachedSkill]) -> StructuredTool:
    """Build the load_skill tool over an already-resolved skill list.

    Closes over the bodies (no DB at invoke time — see module docstring). An
    unknown name returns the valid names rather than an error, so a model
    that mistypes can self-correct within the turn.
    """
    by_name = {skill.name: skill for skill in skills}

    async def _load_skill(skill_name: str) -> str:
        skill = by_name.get(skill_name.strip())
        if skill is None:
            available = ", ".join(sorted(by_name))
            return (
                f"No skill named '{skill_name}'. Available skills: {available}. "
                "Call load_skill again with one of these exact names."
            )
        return (
            f"Loaded skill '{skill.name}'. Follow these instructions:\n\n"
            f"{skill.content}"
        )

    names = ", ".join(sorted(by_name))
    return StructuredTool.from_function(
        coroutine=_load_skill,
        name=LOAD_SKILL_TOOL_NAME,
        description=(
            "Load the full instructions of an attached skill by name. "
            f"Available: {names}. Call this before performing a task a "
            "skill's description covers."
        ),
        args_schema=LoadSkillInput,
    )
