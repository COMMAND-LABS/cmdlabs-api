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
