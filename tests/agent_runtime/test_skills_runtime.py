"""
Agent Skills at runtime: index injection, the load_skill tool, and the
loader's failure directions (fail-soft on stale refs, fail-closed on
entitlement).

Layered like test_tool_entitlement.py: pure pieces first (no DB), then the
loader against real rows via the top-level db fixture.
"""
import pytest
from sqlalchemy.orm import Session

from src.agent_runtime.skills import (
    AttachedSkill,
    LOAD_SKILL_TOOL_NAME,
    build_skills_guidance,
    create_load_skill_tool,
    expand_slash_command,
    load_agent_skills,
)
from src.db.models import Agent, Skill
from src.services.org_scope import OrgScope
from tests.org_isolation import make_tenant

SKILLS = [
    AttachedSkill(name="brand-voice", description="How to write in the company voice.",
                  content="# Brand voice\n\nWrite plainly."),
    AttachedSkill(name="sql-review", description="Checklist for reviewing SQL.",
                  content="# SQL review\n\nCheck indexes."),
]


# ---------------------------------------------------------------------------
# the prompt index
# ---------------------------------------------------------------------------

def test_guidance_lists_every_skill():
    guidance = build_skills_guidance(SKILLS)
    assert "<available_skills>" in guidance
    assert "brand-voice: How to write in the company voice." in guidance
    assert "sql-review" in guidance
    assert LOAD_SKILL_TOOL_NAME in guidance


def test_guidance_is_brace_escaped():
    """User-authored names/descriptions feed a LangChain f-string template;
    an unescaped brace would parse as a variable and crash the turn."""
    braced = [AttachedSkill(name="jinja-help",
                            description="Render {{ user.name }} templates",
                            content="body")]
    guidance = build_skills_guidance(braced)
    # Every brace doubled: the description's {{ }} arrive as {{{{ }}}}.
    assert "{{{{ user.name }}}}" in guidance
    # De-escaping (what LangChain's renderer does) restores the original.
    assert "{{ user.name }} templates" in guidance.replace("{{", "{").replace("}}", "}")


def test_guidance_empty_for_no_skills():
    assert build_skills_guidance([]) == ""


# ---------------------------------------------------------------------------
# the load_skill tool
# ---------------------------------------------------------------------------

async def test_load_skill_returns_body():
    tool = create_load_skill_tool(SKILLS)
    assert tool.name == LOAD_SKILL_TOOL_NAME
    assert "brand-voice" in tool.description
    result = await tool.coroutine(skill_name="brand-voice")
    assert "Write plainly." in result


async def test_load_skill_unknown_name_lists_options():
    """A mistyped name gets the valid names back, so the model can
    self-correct within the turn instead of erroring out of it."""
    tool = create_load_skill_tool(SKILLS)
    result = await tool.coroutine(skill_name="brand-vioce")
    assert "No skill named" in result
    assert "brand-voice" in result and "sql-review" in result


async def test_load_skill_tolerates_whitespace():
    tool = create_load_skill_tool(SKILLS)
    result = await tool.coroutine(skill_name="  brand-voice ")
    assert "Write plainly." in result


# ---------------------------------------------------------------------------
# slash commands (explicit invocation)
# ---------------------------------------------------------------------------

def test_slash_expands_with_args():
    expanded = expand_slash_command("/sql-review the orders migration", SKILLS)
    assert expanded is not None
    assert "'/sql-review'" in expanded
    assert "Check indexes." in expanded
    assert "the orders migration" in expanded


def test_slash_expands_without_args():
    expanded = expand_slash_command("/brand-voice", SKILLS)
    assert expanded is not None
    assert "Write plainly." in expanded
    assert "User input" not in expanded


def test_slash_newline_ends_the_command_token():
    expanded = expand_slash_command("/sql-review\ncheck the orders\nmigration", SKILLS)
    assert expanded is not None
    assert "Check indexes." in expanded
    assert "check the orders\nmigration" in expanded


def test_slash_unknown_command_passes_through():
    assert expand_slash_command("/no-such-skill do it", SKILLS) is None


def test_slash_requires_exact_name():
    """'/sql' must not fuzzy-match 'sql-review' — near-misses fall back to the
    model-side index rather than silently running the wrong skill."""
    assert expand_slash_command("/sql review this", SKILLS) is None


def test_slash_ordinary_message_untouched():
    assert expand_slash_command("what does /etc/hosts do?", SKILLS) is None
    assert expand_slash_command("plain message", SKILLS) is None
    assert expand_slash_command("/", SKILLS) is None


def test_slash_no_skills_passes_through():
    """The caller hands in the already entitlement-filtered list; empty means
    a matching-looking command is just text."""
    assert expand_slash_command("/brand-voice hello", []) is None


def test_slash_body_is_not_brace_escaped():
    """The expansion rides in the {input} template VARIABLE, so a skill body
    with braces must arrive verbatim — doubling them here would show the
    model literal {{ }}."""
    braced = [AttachedSkill(name="tmpl", description="d",
                            content="Render {{ user.name }} like this")]
    expanded = expand_slash_command("/tmpl", braced)
    assert expanded is not None
    assert "Render {{ user.name }} like this" in expanded


# ---------------------------------------------------------------------------
# the loader (real rows)
# ---------------------------------------------------------------------------

def _make_agent(db, tenant, skill_ids):
    agent = Agent(
        org_id=tenant.org_id,
        account_id=tenant.account_id,
        name="Skilled",
        config={
            "schema": "agent_config",
            "version": 4,
            "data": {
                "systemPrompt": "hi",
                "skills": [{"skillId": sid} for sid in skill_ids],
            },
        },
    )
    db.add(agent)
    db.flush()
    return agent


def _scope(tenant) -> OrgScope:
    return OrgScope(account_id=tenant.account_id, org_id=tenant.org_id)


def test_loader_resolves_in_config_order(db: Session):
    t = make_tenant(db, slug="skl-order", account_id=5501)
    a = Skill(org_id=t.org_id, account_id=t.account_id, name="alpha",
              description="a", content="A")
    b = Skill(org_id=t.org_id, account_id=t.account_id, name="beta",
              description="b", content="B")
    db.add_all([a, b]); db.flush()

    agent = _make_agent(db, t, [b.id, a.id])
    loaded = load_agent_skills(db, agent, _scope(t))
    assert [s.name for s in loaded] == ["beta", "alpha"]


def test_loader_skips_stale_refs(db: Session):
    """A deleted skill degrades the agent, never kills it."""
    t = make_tenant(db, slug="skl-stale", account_id=5502)
    real = Skill(org_id=t.org_id, account_id=t.account_id, name="real",
                 description="d", content="C")
    db.add(real); db.flush()

    agent = _make_agent(db, t, [real.id, 987654])
    loaded = load_agent_skills(db, agent, _scope(t))
    assert [s.name for s in loaded] == ["real"]


def test_loader_visibility_follows_the_agent_owner(db: Session):
    """A colleague's PRIVATE skill is unreachable even if its id lands in the
    config; their 'org' skill attaches fine."""
    owner = make_tenant(db, slug="skl-vis", account_id=5503)
    colleague = make_tenant(db, slug="skl-vis", account_id=5504)
    private = Skill(org_id=colleague.org_id, account_id=colleague.account_id,
                    name="their-private", description="d", content="P",
                    visibility="private")
    shared = Skill(org_id=colleague.org_id, account_id=colleague.account_id,
                   name="their-shared", description="d", content="S",
                   visibility="org")
    db.add_all([private, shared]); db.flush()

    agent = _make_agent(db, owner, [private.id, shared.id])
    loaded = load_agent_skills(db, agent, _scope(owner))
    assert [s.name for s in loaded] == ["their-shared"]


def test_loader_never_crosses_orgs(db: Session):
    """Even a config that somehow carries a foreign id resolves nothing —
    the runtime predicate is the backstop behind write-time validation."""
    mine = make_tenant(db, slug="skl-mine", account_id=5505)
    theirs = make_tenant(db, slug="skl-theirs", account_id=5506)
    foreign = Skill(org_id=theirs.org_id, account_id=theirs.account_id,
                    name="foreign", description="d", content="F",
                    visibility="org")
    db.add(foreign); db.flush()

    agent = _make_agent(db, mine, [foreign.id])
    assert load_agent_skills(db, agent, _scope(mine)) == []


def test_loader_fails_closed_without_entitlement(db: Session, monkeypatch):
    """No 'skills' module for the caller → no index, no tool. Same reasoning
    as tool entitlement: the runtime must not be the way around
    require_module()."""
    t = make_tenant(db, slug="skl-gate", account_id=5507)
    skill = Skill(org_id=t.org_id, account_id=t.account_id, name="gated",
                  description="d", content="G")
    db.add(skill); db.flush()
    agent = _make_agent(db, t, [skill.id])

    monkeypatch.setattr(
        "src.agent_runtime.skills.effective_modules",
        lambda db_, account_id, org_id: {"agents"},
    )
    assert load_agent_skills(db, agent, _scope(t)) == []


def test_loader_handles_agentless_path(db: Session):
    """The contact-chat override path has no agent row — no skills, no error."""
    t = make_tenant(db, slug="skl-none", account_id=5508)
    assert load_agent_skills(db, None, _scope(t)) == []


# ---------------------------------------------------------------------------
# save_skill (skill-creator)
# ---------------------------------------------------------------------------

from src.agent_runtime.skills import (  # noqa: E402
    SAVE_SKILL_TOOL_NAME,
    SKILL_CREATOR_NAME,
    create_save_skill_tool,
    has_skill_creator,
)


class _NoCloseSession:
    """Hands the test's transactional session to the tool as if it were a
    fresh one: commit flushes, close is a no-op, so the fixture's rollback
    still owns the data."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def commit(self):
        self._db.flush()

    def close(self):
        pass


def _save_tool(db, tenant):
    return create_save_skill_tool(_scope(tenant), session_factory=lambda: _NoCloseSession(db))


def test_has_skill_creator_is_name_based():
    assert not has_skill_creator(SKILLS)
    assert has_skill_creator(SKILLS + [AttachedSkill(name=SKILL_CREATOR_NAME, description="d", content="c")])


@pytest.mark.asyncio
async def test_save_skill_creates_private_skill_owned_by_caller(db: Session):
    t = make_tenant(db, slug="skl-save", account_id=5601)
    tool = _save_tool(db, t)
    assert tool.name == SAVE_SKILL_TOOL_NAME

    result = await tool.coroutine(
        name="contract-redline",
        description="Use when reviewing a contract.",
        content="# Redline\n\nFlag indemnity clauses.",
    )
    assert result["saved"] is True
    assert result["action"] == "created"

    row = db.get(Skill, result["skillId"])
    assert row.org_id == t.org_id
    assert row.account_id == t.account_id
    assert row.visibility == "private"
    assert row.content == "# Redline\n\nFlag indemnity clauses."


@pytest.mark.asyncio
async def test_save_skill_strips_front_matter_if_model_sends_it(db: Session):
    t = make_tenant(db, slug="skl-fm", account_id=5602)
    result = await _save_tool(db, t).coroutine(
        name="fm-skill", description="d",
        content="---\nname: fm-skill\nversion: 2\n---\n# Body\n",
    )
    assert result["saved"] is True
    row = db.get(Skill, result["skillId"])
    assert row.content == "# Body\n"
    assert row.frontmatter == {"name": "fm-skill", "version": 2}


@pytest.mark.asyncio
async def test_save_skill_rejects_bad_name_as_tool_result(db: Session):
    t = make_tenant(db, slug="skl-bad", account_id=5603)
    result = await _save_tool(db, t).coroutine(
        name="Not Kebab", description="d", content="body",
    )
    assert result["saved"] is False
    assert "kebab" in result["error"].lower() or "name" in result["error"].lower()


@pytest.mark.asyncio
async def test_save_skill_refuses_silent_overwrite(db: Session):
    t = make_tenant(db, slug="skl-dup", account_id=5604)
    existing = Skill(org_id=t.org_id, account_id=t.account_id, name="dup",
                     description="old", content="OLD")
    db.add(existing); db.flush()

    tool = _save_tool(db, t)
    result = await tool.coroutine(name="dup", description="new", content="NEW")
    assert result["saved"] is False
    assert "overwrite" in result["error"]
    assert result["skillId"] == existing.id
    db.refresh(existing)
    assert existing.content == "OLD"

    result = await tool.coroutine(name="dup", description="new", content="NEW", overwrite=True)
    assert result["saved"] is True
    assert result["action"] == "updated"
    db.refresh(existing)
    assert existing.content == "NEW"
    assert existing.description == "new"


@pytest.mark.asyncio
async def test_save_skill_never_overwrites_a_colleagues_skill(db: Session):
    owner = make_tenant(db, slug="skl-col", account_id=5605)
    caller = make_tenant(db, slug="skl-col", account_id=5606)
    theirs = Skill(org_id=owner.org_id, account_id=owner.account_id, name="theirs",
                   description="d", content="THEIRS", visibility="org")
    db.add(theirs); db.flush()

    result = await _save_tool(db, caller).coroutine(
        name="theirs", description="x", content="MINE", overwrite=True,
    )
    assert result["saved"] is False
    assert "another member" in result["error"]
    db.refresh(theirs)
    assert theirs.content == "THEIRS"
