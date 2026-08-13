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
