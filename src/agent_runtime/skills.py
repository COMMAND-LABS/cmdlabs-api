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

save_skill — the one WRITE in this module — is the exception: it opens its
own short-lived session per call (tools/sessions.py pattern). It exists so a
"skill-creator" skill can persist what it drafts, and it is only built when a
skill with that name is attached: the capability travels with the skill that
knows how to use it rather than appearing on every skilled agent. Created
rows belong to the CALLER (not the agent owner) and are private by default —
a colleague running a shared agent authors into their own space, and nothing
becomes org-visible without a deliberate act in the skills UI.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_

from src.agent_runtime.tool_entitlement import effective_modules
from src.agent_runtime.tools.sessions import default_session_factory
from src.db.models import Skill
from src.services.org_scope import OrgScope

logger = logging.getLogger(__name__)

SKILLS_MODULE_KEY = "skills"

LOAD_SKILL_TOOL_NAME = "load_skill"
SAVE_SKILL_TOOL_NAME = "save_skill"

# The attached-skill name that unlocks save_skill. Matches the built-in
# template (src/skill_templates/skill-creator.md); an org may also hand-write
# a skill with this name and get the tool.
SKILL_CREATOR_NAME = "skill-creator"


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


def expand_slash_command(prompt: str, skills: list[AttachedSkill]) -> str | None:
    """Deterministic skill invocation: '/name rest' → expanded turn input.

    When the user's message begins with a slash command naming an attached
    skill, the skill body is injected directly into the turn instead of
    waiting for the model to elect load_skill — an explicit invocation must
    not be subject to model discretion.

    Returns None whenever the message is not a match (no leading slash, or a
    first token that names no attached skill) so an ordinary message that
    happens to start with '/' passes through untouched. Matching is exact:
    names are kebab-case handles and near-misses fall back to the model-side
    index, where load_skill's unknown-name reply lets it self-correct.

    Only the EXPANDED text goes to the model; the caller keeps persisting the
    raw prompt, so the transcript shows what the user actually typed.
    """
    if not skills:
        return None
    # First token after the slash is the command; whatever follows any
    # whitespace (space or newline) is the arguments.
    match = re.match(r"/(\S+)(?:\s+([\s\S]*))?$", prompt)
    if match is None:
        return None
    command, args = match.group(1), match.group(2) or ""
    skill = next((s for s in skills if s.name == command), None)
    if skill is None:
        return None
    expanded = (
        f"The user explicitly invoked the skill '/{skill.name}'. "
        f"Follow these instructions now:\n\n{skill.content}"
    )
    args = args.strip()
    if args:
        expanded += f"\n\nUser input for this invocation:\n{args}"
    return expanded


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


# ---------------------------------------------------------------------------
# save_skill (skill-creator only)
# ---------------------------------------------------------------------------

def has_skill_creator(skills: list[AttachedSkill]) -> bool:
    return any(skill.name == SKILL_CREATOR_NAME for skill in skills)


class SaveSkillInput(BaseModel):
    name: str = Field(
        description="Kebab-case skill name, e.g. 'contract-redline'. Unique "
                    "within the organization. Max 64 characters.",
    )
    description: str = Field(
        description="When an agent should load this skill: what it does and "
                    "the requests that should trigger it. Max 1024 characters.",
    )
    content: str = Field(
        description="The markdown body of the skill WITHOUT a YAML front "
                    "matter block (name and description are separate fields).",
    )
    overwrite: bool = Field(
        default=False,
        description="Replace an existing skill with this name that the user "
                    "owns. Only set after the user explicitly asked to "
                    "overwrite.",
    )


def create_save_skill_tool(
    org_scope: OrgScope,
    *,
    session_factory=None,
) -> StructuredTool:
    """Build save_skill bound to the CALLER's org scope.

    Validation reuses the route validators so a skill the model writes obeys
    exactly the limits a skill typed into the UI does. Every failure comes
    back as a tool result the model can act on (fix the name, ask the user
    about overwriting) — never an exception, which would end the turn.
    """
    # Imported here: the routers package pulls in FastAPI app wiring that the
    # runtime module should not load at import time.
    from fastapi import HTTPException

    from src.routers.skills.models import (
        validate_skill_content,
        validate_skill_description,
        validate_skill_name,
    )
    from src.services.skill_markdown import SkillMarkdownError, parse_skill_markdown

    factory = session_factory or default_session_factory

    async def _save_skill(
        name: str, description: str, content: str, overwrite: bool = False,
    ) -> dict:
        try:
            name = validate_skill_name(name.strip())
            description = validate_skill_description(description.strip())
            content = validate_skill_content(content)
            # The model was told not to send front matter, but if it does,
            # honor it the way the create route would rather than storing a
            # stray '---' block as body text.
            frontmatter, body = parse_skill_markdown(content)
            body = validate_skill_content(body)
        except HTTPException as exc:
            return {"saved": False, "error": str(exc.detail)}
        except SkillMarkdownError as exc:
            return {"saved": False, "error": str(exc)}

        db = factory()
        try:
            existing = db.query(Skill).filter(
                Skill.org_id == org_scope.org_id, Skill.name == name,
            ).first()
            if existing is not None:
                if not overwrite:
                    return {
                        "saved": False,
                        "error": (
                            f"A skill named '{name}' already exists. Ask the "
                            "user whether to overwrite it (then call again "
                            "with overwrite=true) or choose a different name."
                        ),
                        "skillId": existing.id,
                    }
                if existing.account_id != org_scope.account_id:
                    return {
                        "saved": False,
                        "error": (
                            f"'{name}' belongs to another member of the "
                            "organization and cannot be overwritten. Choose "
                            "a different name."
                        ),
                    }
                existing.description = description
                existing.content = body
                existing.frontmatter = frontmatter or None
                db.commit()
                db.refresh(existing)
                logger.info("[SKILLS] save_skill updated skill %s (%s)", existing.id, name)
                return {
                    "saved": True,
                    "action": "updated",
                    "skillId": existing.id,
                    "name": name,
                    "visibility": existing.visibility,
                    "message": (
                        f"Updated skill '{name}'. Agents that have it attached "
                        "will use the new version on their next turn."
                    ),
                }

            skill = Skill(
                org_id=org_scope.org_id,
                account_id=org_scope.account_id,
                name=name,
                description=description,
                content=body,
                frontmatter=frontmatter or None,
                visibility="private",
            )
            db.add(skill)
            db.commit()
            db.refresh(skill)
            logger.info("[SKILLS] save_skill created skill %s (%s)", skill.id, name)
            return {
                "saved": True,
                "action": "created",
                "skillId": skill.id,
                "name": name,
                "visibility": "private",
                "message": (
                    f"Created private skill '{name}'. The user must attach it "
                    "to an agent (agent settings → Skills) before that agent "
                    f"can use it; it can then be invoked with /{name}."
                ),
            }
        except Exception as exc:  # noqa: BLE001 — a tool must not end the turn
            db.rollback()
            logger.exception("[SKILLS] save_skill failed for %s", name)
            return {"saved": False, "error": f"Could not save skill: {exc}"}
        finally:
            db.close()

    return StructuredTool.from_function(
        coroutine=_save_skill,
        name=SAVE_SKILL_TOOL_NAME,
        description=(
            "Save a skill the user has approved as a new private skill in "
            "their organization. Call only after the user confirmed the "
            "draft. Returns the saved skill's id, or an error to act on."
        ),
        args_schema=SaveSkillInput,
    )
