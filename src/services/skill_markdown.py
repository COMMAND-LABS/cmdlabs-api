"""
SKILL.md parsing: YAML front matter in, (metadata, body) out.

A skill arrives as markdown that MAY open with a front matter block:

    ---
    name: brand-voice
    description: How to write in the company voice.
    ---
    # Brand voice
    ...

Parsed with yaml.safe_load — NOT the hand-rolled key:value scanner in the
txt-ingest cloud function, which silently mis-parses lists and nested maps.
PyYAML is already a dependency of this service.

Malformed YAML raises SkillMarkdownError rather than degrading to "no front
matter": a document that clearly tried to carry metadata and failed should
tell its author, not quietly store the broken block as body text where the
model would read it.

Keeping this Anthropic-compatible is deliberate: it makes a later
import/export of SKILL.md folders a packaging problem, not a format change.
"""
from typing import Any

import yaml

FRONTMATTER_DELIMITER = "---"


class SkillMarkdownError(ValueError):
    """User-facing parse failure; routes surface str(exc) as a 400 detail."""


def parse_skill_markdown(content: str) -> tuple[dict[str, Any], str]:
    """Split a skill document into (front matter mapping, markdown body).

    No front matter (or an empty block) returns ({}, content) unchanged.
    The body keeps its own leading whitespace semantics: one leading newline
    after the closing delimiter is dropped, nothing else is touched.
    """
    if not content.startswith(FRONTMATTER_DELIMITER + "\n") and content.strip() != FRONTMATTER_DELIMITER:
        return {}, content

    # First line is the opening '---'; find the closing delimiter line.
    lines = content.split("\n")
    closing = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FRONTMATTER_DELIMITER:
            closing = i
            break
    if closing is None:
        raise SkillMarkdownError(
            "Front matter opened with '---' but never closed. "
            "Add a closing '---' line, or remove the opening one."
        )

    raw = "\n".join(lines[1:closing])
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SkillMarkdownError(f"Front matter is not valid YAML: {exc}") from exc

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise SkillMarkdownError(
            "Front matter must be a YAML mapping (key: value lines), "
            f"got {type(parsed).__name__}."
        )

    body = "\n".join(lines[closing + 1:])
    if body.startswith("\n"):
        body = body[1:]
    return parsed, body


def serialize_skill_markdown(frontmatter: dict[str, Any] | None, body: str) -> str:
    """Reassemble a full SKILL.md document from stored pieces.

    The inverse of parse_skill_markdown up to YAML formatting (key order is
    preserved; comments are not, since only the parsed mapping is stored).
    """
    if not frontmatter:
        return body
    block = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip("\n")
    return f"{FRONTMATTER_DELIMITER}\n{block}\n{FRONTMATTER_DELIMITER}\n{body}"
