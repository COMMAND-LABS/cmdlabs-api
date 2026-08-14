"""The think tool: multi-step turns without external tools.

The AgentExecutor loop only activates when the tools list is non-empty, and
only iterates while the model emits tool calls — so a toolless agent is
structurally single-call. The think tool exists to give that loop something to
chew on. What matters, and what is asserted here:

- it is registered and explicitly classified as UNGATED (it must survive
  entitlement filtering for every caller, or multi-step silently degrades);
- the builder needs no credential and the tool performs no I/O;
- the v4 config schema accepts it and still rejects garbage.
"""

import pytest

from src.agent_runtime.tool_entitlement import TOOL_MODULES, allowed_tool_configs
from src.agent_runtime.tools.registry import ToolRegistry
from src.agent_runtime.tools.think import (
    THINK_SYSTEM_GUIDANCE,
    create_think_tool,
)
from src.schemas import validate_against_schema
from jsonschema import ValidationError


# ── Registration & entitlement ──────────────────────────────────────────────

def test_think_is_registered():
    assert callable(ToolRegistry.get_builder("think"))


def test_think_is_explicitly_ungated():
    # In TOOL_MODULES (the map lists every type so omissions are visible),
    # mapped to None (no module gate).
    assert "think" in TOOL_MODULES
    assert TOOL_MODULES["think"] is None


def test_think_survives_entitlement_with_no_modules_granted():
    """The whole point: it must work for a caller who can open nothing."""
    kept = allowed_tool_configs([{"type": "think"}], granted=set())
    assert kept == [{"type": "think"}]


# ── The tool itself ─────────────────────────────────────────────────────────

async def test_builder_produces_a_working_tool():
    tool = await create_think_tool(tool_config={"type": "think"},
                                   account_id=1, db=None)
    assert tool.name == "think"
    result = await tool.coroutine(thought="step one: consider the problem")
    assert isinstance(result, str) and result


async def test_builder_honours_custom_description():
    tool = await create_think_tool(
        tool_config={"type": "think", "description": "Plan your moves."},
        account_id=1, db=None,
    )
    assert tool.description == "Plan your moves."


def test_guidance_is_template_safe():
    """The system prompt is brace-escaped BEFORE the guidance is appended, so
    literal braces here would reach ChatPromptTemplate unescaped and break
    formatting on every multi-step turn."""
    assert "{" not in THINK_SYSTEM_GUIDANCE
    assert "}" not in THINK_SYSTEM_GUIDANCE


# ── Schema ──────────────────────────────────────────────────────────────────

def _config(data_extra: dict) -> dict:
    return {"schema": "agent_config", "version": 4,
            "data": {"systemPrompt": "You are helpful.", **data_extra}}


def test_schema_accepts_think_tool():
    validate_against_schema(_config({"tools": [{"type": "think"}]}),
                            "agent_config", 4)


def test_schema_accepts_think_tool_with_description():
    validate_against_schema(
        _config({"tools": [{"type": "think", "description": "Reason first."}]}),
        "agent_config", 4)


def test_schema_rejects_unknown_tool_type():
    with pytest.raises(ValidationError):
        validate_against_schema(_config({"tools": [{"type": "daydream"}]}),
                                "agent_config", 4)


def test_schema_rejects_think_with_extra_fields():
    with pytest.raises(ValidationError):
        validate_against_schema(
            _config({"tools": [{"type": "think", "credentialId": 3}]}),
            "agent_config", 4)
