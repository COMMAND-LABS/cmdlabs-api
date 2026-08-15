"""Unit tests for SKILL.md front-matter parsing (services/skill_markdown.py)."""

import pytest

from src.services.skill_markdown import (
    SkillMarkdownError,
    parse_skill_markdown,
    serialize_skill_markdown,
)

FULL_DOC = """---
name: brand-voice
description: How to write in the company voice.
tags:
  - writing
  - marketing
---
# Brand voice

Always write plainly.
"""


def test_parses_front_matter_and_body():
    frontmatter, body = parse_skill_markdown(FULL_DOC)
    assert frontmatter["name"] == "brand-voice"
    assert frontmatter["description"] == "How to write in the company voice."
    # Real YAML, not the naive key:value scanner — lists parse as lists.
    assert frontmatter["tags"] == ["writing", "marketing"]
    assert body.startswith("# Brand voice")
    assert "---" not in body


def test_no_front_matter_passes_through():
    content = "# Just markdown\n\nNo metadata here."
    frontmatter, body = parse_skill_markdown(content)
    assert frontmatter == {}
    assert body == content


def test_unclosed_front_matter_raises():
    with pytest.raises(SkillMarkdownError, match="never closed"):
        parse_skill_markdown("---\nname: x\n# body without closing delimiter")


def test_malformed_yaml_raises():
    with pytest.raises(SkillMarkdownError, match="not valid YAML"):
        parse_skill_markdown("---\nname: [unclosed\n---\nbody")


def test_non_mapping_front_matter_raises():
    with pytest.raises(SkillMarkdownError, match="mapping"):
        parse_skill_markdown("---\n- just\n- a list\n---\nbody")


def test_empty_front_matter_block():
    frontmatter, body = parse_skill_markdown("---\n---\nbody")
    assert frontmatter == {}
    assert body == "body"


def test_round_trip():
    frontmatter, body = parse_skill_markdown(FULL_DOC)
    rebuilt = serialize_skill_markdown(frontmatter, body)
    frontmatter2, body2 = parse_skill_markdown(rebuilt)
    assert frontmatter2 == frontmatter
    assert body2 == body


def test_serialize_without_front_matter_is_identity():
    assert serialize_skill_markdown(None, "body") == "body"
    assert serialize_skill_markdown({}, "body") == "body"
