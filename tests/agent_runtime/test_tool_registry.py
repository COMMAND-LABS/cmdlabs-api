"""Tests for the tool registry and factory."""

from src.agent_runtime.tools.registry import ToolRegistry


def test_all_expected_types_registered():
    expected = [
        "vectorSearch",
        "vectorSearchWithReranking",
        "dbTableRead",
        "dbTableWrite",
        "sendTxtEmailWithSes",
        "sendHtmlEmailWithSes",
    ]
    registered = ToolRegistry.list_types()
    for t in expected:
        assert t in registered, f"Missing tool type: {t}"


def test_get_builder_returns_callable():
    builder = ToolRegistry.get_builder("vectorSearch")
    assert callable(builder)


def test_get_builder_returns_none_for_unknown():
    assert ToolRegistry.get_builder("nonexistent_tool") is None


async def test_built_tools_carry_their_config_type():
    """The factory stamps each tool with its config type, so a renamed tool
    is still recognisable; skill tools are stamped by their builders."""
    from langchain_core.tools import StructuredTool

    from src.agent_runtime.skills import AttachedSkill, create_load_skill_tool
    from src.agent_runtime.tools.factory import _build_tool
    from src.agent_runtime.tools.registry import tool_types_by_name

    async def fake_builder(tool_config, **kwargs):
        return StructuredTool.from_function(
            func=lambda: "x", name=tool_config["name"], description="d")

    ToolRegistry.register("fakeType", fake_builder)
    try:
        tool = await _build_tool({"type": "fakeType", "name": "renamed_tool"},
                                 account_id=1, db=None)
    finally:
        ToolRegistry._builders.pop("fakeType", None)

    load_skill = create_load_skill_tool([AttachedSkill(name="s", description="d", content="c")])
    assert tool_types_by_name([tool, load_skill]) == {
        "renamed_tool": "fakeType", "load_skill": "loadSkill"}


def _schema_tool_types() -> set[str]:
    """Every tool `type` the agent config schema accepts (its $defs.tool oneOf)."""
    import json
    from pathlib import Path

    import src.schemas as schemas_pkg

    schema = json.loads(
        (Path(schemas_pkg.__file__).parent / "agent_config.v4.json").read_text())
    defs = schema["$defs"]
    types = set()
    for option in defs["tool"]["oneOf"]:
        name = option["$ref"].rsplit("/", 1)[-1]
        types.add(defs[name]["properties"]["type"]["const"])
    return types


def test_every_schema_tool_type_can_be_built_and_is_gated():
    """A type the schema accepts must have a builder AND a module decision.

    Drift guard: the Gmail email types were accepted by the schema, offered by
    the UI and sendable by the approval step, but had no builder, so the
    factory dropped them as "unknown" and agents silently ran without them.
    """
    from src.agent_runtime.tool_entitlement import TOOL_MODULES

    schema_types = _schema_tool_types()
    assert schema_types, "parsed no tool types from the schema"
    assert schema_types - set(ToolRegistry.list_types()) == set(), "schema types with no builder"
    assert schema_types - set(TOOL_MODULES) == set(), "schema types missing from TOOL_MODULES"


async def test_gmail_email_tools_build_from_a_complete_credential(monkeypatch):
    from src.agent_runtime.tools import hitl_email_base

    seen = {}

    def fake_verify(credential_id, account_id, db, required, label):
        seen[label] = required
        return "me@example.com"

    monkeypatch.setattr(hitl_email_base, "verify_credential", fake_verify)
    for tool_type, label, name in (
        ("sendTxtEmailWithGoogleOAuth", "Google OAuth", "send_txt_email_with_google_oauth"),
        ("sendTxtEmailWithGoogleSmtp", "Gmail SMTP", "send_txt_email_with_google_smtp"),
    ):
        builder = ToolRegistry.get_builder(tool_type)
        tool = await builder(tool_config={"type": tool_type, "credentialId": 7},
                             account_id=1, db=None)
        assert tool.name == name
    # The fields approve.py needs to actually send with each provider.
    assert seen["Google OAuth"] == ["client_id", "client_secret", "refresh_token", "from_email"]
    assert seen["Gmail SMTP"] == ["from_email", "app_password"]
