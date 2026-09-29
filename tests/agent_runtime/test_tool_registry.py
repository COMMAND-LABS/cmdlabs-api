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
