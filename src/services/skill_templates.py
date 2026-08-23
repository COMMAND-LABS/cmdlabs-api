"""
Built-in skill templates: SKILL.md files shipped with the service that an org
can install as its own editable skill rows.

The first (and, in v1, only) template is `skill-creator` — the skill that
teaches an agent to author other skills. It ships as a file rather than a
seed migration because the text will be iterated on, and a template is a
starting point the org owns afterwards, not a shared row: installing copies
it, so later edits to the file never rewrite an org's customized version.

Files live in src/skill_templates/<name>.md in Anthropic SKILL.md format and
go through the same parser as user uploads (services/skill_markdown.py), so a
template that would not round-trip as a user-created skill fails loudly at
load time instead of installing something the routes would have rejected.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from src.services.skill_markdown import parse_skill_markdown

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "skill_templates"

SKILL_CREATOR_TEMPLATE_NAME = "skill-creator"


@dataclass(frozen=True)
class SkillTemplate:
    name: str
    description: str
    content: str
    frontmatter: dict


@lru_cache(maxsize=1)
def load_skill_templates() -> dict[str, SkillTemplate]:
    """Parse every template file once per process, keyed by name.

    The front matter's `name` wins over the filename so the handle the model
    uses is the one the author wrote, but the two are expected to agree.
    """
    templates: dict[str, SkillTemplate] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        frontmatter, body = parse_skill_markdown(raw)
        name = frontmatter.get("name") or path.stem
        description = frontmatter.get("description")
        if not description:
            raise ValueError(f"Skill template {path.name} has no description")
        templates[name] = SkillTemplate(
            name=name,
            description=str(description).strip(),
            content=body,
            frontmatter=frontmatter,
        )
    return templates


def get_skill_template(name: str) -> SkillTemplate | None:
    return load_skill_templates().get(name)
