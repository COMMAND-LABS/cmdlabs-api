"""
Tool Registry

A simple dict-backed registry mapping tool type strings to their async builder
functions. Builders are registered at startup (see tools/__init__.py) and looked
up at runtime by the factory.
"""
from collections.abc import Callable

from langchain_core.tools import StructuredTool

ToolBuilder = Callable[..., StructuredTool]


class ToolRegistry:
    """Registry for tool builders keyed by tool type string."""

    _builders: dict[str, ToolBuilder] = {}

    @classmethod
    def register(cls, tool_type: str, builder: ToolBuilder) -> None:
        cls._builders[tool_type] = builder

    @classmethod
    def get_builder(cls, tool_type: str) -> ToolBuilder | None:
        return cls._builders.get(tool_type)

    @classmethod
    def list_types(cls) -> list[str]:
        return list(cls._builders.keys())


# ---------------------------------------------------------------------------
# Tool type on a BUILT tool
# ---------------------------------------------------------------------------
# A tool's `name` is what the model calls and is free per agent config
# ("forecast_duty_spend"); its TYPE is what it is ("timeSeriesForecast"). The
# type used to be guessed back from the name, which failed for every renamed
# tool. Now whoever builds a tool stamps its type on it, and the stream reads
# it from there (helpers/tool_calls.format_tool_call, stream.on_tool_start).

TOOL_TYPE_KEY = "tool_type"


def tag_tool_type(tool: StructuredTool, tool_type: str) -> StructuredTool:
    tool.metadata = {**(tool.metadata or {}), TOOL_TYPE_KEY: tool_type}
    return tool


def tool_types_by_name(tools: list[StructuredTool]) -> dict[str, str]:
    """{tool name: tool type} for every tool that was tagged."""
    return {
        t.name: (t.metadata or {})[TOOL_TYPE_KEY]
        for t in tools
        if (t.metadata or {}).get(TOOL_TYPE_KEY)
    }
