"""Think Tool — internal reasoning steps, no external access.

The smallest tool in the registry, and deliberately so: it performs no I/O,
needs no credential, and returns a fixed acknowledgment. Its entire value is
STRUCTURAL — the AgentExecutor loop only iterates while the model emits tool
calls, so a toolless agent finishes on its first response. Registering this
gives the model something to call between reasoning steps, which is what turns
"one completion per turn" into a multi-step turn (the "think tool" pattern).

Each think call is one executor iteration, bounded by the shared
max_iterations like every other tool, and persisted in the turn's tool_calls
so the transcript shows the reasoning steps.
"""

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

DEFAULT_DESCRIPTION = (
    "Use this tool to think through a problem step by step before answering. "
    "Write out your reasoning about what you know so far and what to do next. "
    "The thought is recorded for your own reference; it performs no action "
    "and returns no new information. Call it as many times as you need, then "
    "give your final answer."
)


# Appended to the system prompt whenever the think tool is present. Kept here
# so the tool and the instructions that make models actually use it stay one
# unit. MUST NOT contain literal braces: the system prompt is brace-escaped
# before this is appended (it feeds a ChatPromptTemplate).
THINK_SYSTEM_GUIDANCE = (
    "\n\nYou have a 'think' tool for working through problems in steps. For "
    "any non-trivial request, use it: call think to lay out your reasoning, "
    "as many times as you need, and only then give your final answer. Your "
    "thoughts are working notes, not the reply — the user sees them as "
    "intermediate steps."
)


class ThinkInput(BaseModel):
    thought: str = Field(
        description="Your reasoning for this step — what you know, what "
                    "follows from it, and what you plan to do next.",
    )


async def create_think_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Any,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    """Build the think tool. The unused parameters keep the shared builder
    signature — this is the one builder with nothing to configure."""

    async def _think(thought: str) -> str:
        return "Thought recorded. Continue reasoning, or give your final answer."

    return StructuredTool.from_function(
        coroutine=_think,
        name="think",
        description=tool_config.get("description") or DEFAULT_DESCRIPTION,
        args_schema=ThinkInput,
    )
