"""Tests for template variable resolution."""

from src.utils.template_variables import (
    SUPPORTED_VARIABLES,
    build_variable_context,
    resolve_template_variables,
)


def test_supported_variables_populated():
    ctx = build_variable_context(agent_name="TestBot")
    for var in SUPPORTED_VARIABLES:
        assert var in ctx, f"Missing variable: {var}"


def test_resolve_known_variable():
    ctx = {"agent_name": "Kalygo"}
    result = resolve_template_variables("Hello {{ agent_name }}!", ctx)
    assert result == "Hello Kalygo!"


def test_resolve_preserves_unknown_variables():
    ctx = {"agent_name": "Bot"}
    result = resolve_template_variables("{{ agent_name }} says {{ unknown_var }}", ctx)
    assert result == "Bot says {{ unknown_var }}"


def test_resolve_handles_no_variables():
    result = resolve_template_variables("No variables here", {})
    assert result == "No variables here"


def test_resolve_handles_spacing_variants():
    ctx = {"current_date": "2025-01-01"}
    assert resolve_template_variables("{{current_date}}", ctx) == "2025-01-01"
    assert resolve_template_variables("{{ current_date }}", ctx) == "2025-01-01"
    assert resolve_template_variables("{{  current_date  }}", ctx) == "2025-01-01"


def test_context_has_time_fields():
    ctx = build_variable_context()
    assert "current_time" in ctx
    assert "current_date" in ctx
    assert "current_datetime" in ctx
    assert "current_day_of_week" in ctx
