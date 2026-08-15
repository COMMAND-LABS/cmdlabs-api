"""Dump the exact prompt a run sends to the model, as one readable file.

WHY THIS EXISTS
---------------
The prompt an agent actually runs on is assembled from four places: the agent's
stored `systemPrompt`, the think-tool guidance appended in context.py, the
session's message history, and the tool schemas — which ride as a provider
parameter rather than as prompt text. LangSmith records all of it, but split
across a span's Input and Tools tabs and repeated once per ReAct iteration, so
reading the whole thing means reassembling it by hand.

This captures it at the only point where it is already whole: `on_chat_model_start`,
the moment LangChain hands the payload over. Nothing here rebuilds the prompt from
config, so nothing can drift from what really ran — which is the entire point.
A reconstruction would be a second implementation of context.py's assembly, and
the first time someone edited one and not the other it would start lying.

WHAT THE ITERATIONS SHOW
------------------------
One section per LLM call. The system message is byte-identical across all of
them; what grows is the tail, as each tool result is appended. That delta IS the
ReAct loop, which is why the sections are written whole rather than diffed —
seeing the same preamble re-sent every time is the thing worth noticing.

OFF BY DEFAULT. A dump contains the agent's full configured prompt and every
message in the session — customer content. Enable per process with
DUMP_AGENT_PROMPT=1 and treat the output directory as sensitive; it is
gitignored, not encrypted.
"""

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

DEFAULT_DUMP_DIR = "scratchspace/prompt-dumps"

# Image and PDF-page blocks are base64 and would bury the text. The prompt
# STRUCTURE is what this file is for; the bytes are not.
_OPAQUE_BLOCKS = {"image", "image_url", "input_audio"}


def _render_content(content: Any) -> str:
    """Flatten a message's content to text, keeping tool calls legible."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            parts.append(str(block))
            continue

        btype = block.get("type")
        if btype == "text":
            parts.append(block.get("text", ""))
        elif btype == "tool_use":
            args = json.dumps(block.get("input", {}), indent=2, default=str)
            parts.append(f"[calls tool: {block.get('name')}]\n{args}")
        elif btype == "tool_result":
            parts.append(f"[tool result]\n{block.get('content')}")
        elif btype in _OPAQUE_BLOCKS:
            parts.append(f"<{btype} content omitted from dump>")
        else:
            parts.append(json.dumps(block, indent=2, default=str))
    return "\n".join(p for p in parts if p)


def _render_tools(tools: list[dict]) -> str:
    """Render bound tool schemas.

    Providers disagree on shape — Anthropic sends {name, description,
    input_schema}, OpenAI wraps the same thing in {type: 'function', function:
    {...}} — so unwrap both rather than assuming the current provider.
    """
    lines: list[str] = []
    for tool in tools:
        spec = tool.get("function", tool) if isinstance(tool, dict) else {}
        name = spec.get("name", "<unnamed>")
        description = (spec.get("description") or "").strip()
        schema = spec.get("input_schema") or spec.get("parameters") or {}

        lines.append(f"### {name}\n")
        if description:
            lines.append(f"{description}\n")
        lines.append("```json")
        lines.append(json.dumps(schema, indent=2, default=str))
        lines.append("```\n")
    return "\n".join(lines)


class PromptDumpCallback(BaseCallbackHandler):
    """Write each LLM call's full payload to a per-session markdown file.

    State is keyed by session id (from the metadata context.py attaches to the
    executor) so concurrent requests never interleave into one file. A single
    instance is therefore safe to share across requests, which is what lets it
    sit in the module-level callbacks list next to the LangSmith tracer.
    """

    def __init__(self, out_dir: str | None = None):
        self.out_dir = Path(out_dir or os.getenv("DUMP_AGENT_PROMPT_DIR", DEFAULT_DUMP_DIR))
        self._runs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id=None,
        parent_run_id=None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        # A dump is a debugging aid. It must never be the reason a user's chat
        # fails, so every failure here is swallowed after being logged.
        try:
            self._dump(messages, metadata or {}, kwargs)
        except Exception:
            logger.exception("[PROMPT-DUMP] failed to write dump; run continues")

    def _dump(self, messages, metadata: dict, kwargs: dict) -> None:
        if not messages or not messages[0]:
            return

        key = str(metadata.get("session_id") or metadata.get("thread_id") or "unknown")

        with self._lock:
            state = self._runs.get(key)
            if state is None:
                state = {"iteration": 0, "path": self._open_file(key, metadata, kwargs)}
                self._runs[key] = state
            state["iteration"] += 1
            iteration = state["iteration"]
            path = state["path"]

        turn = messages[0]
        out = [f"\n## Iteration {iteration} — {len(turn)} messages\n"]
        for i, message in enumerate(turn, start=1):
            role = getattr(message, "type", message.__class__.__name__)
            out.append(f"### {i}. {role}\n")
            out.append("```text")
            out.append(_render_content(getattr(message, "content", "")))
            out.append("```\n")

        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n".join(out))

    def _open_file(self, key: str, metadata: dict, kwargs: dict) -> Path:
        """Create the file and write the header plus the tool schemas.

        Tools are written ONCE. They are re-sent on every iteration and are
        identical each time, so repeating them would triple the file and bury
        the one thing that does change.
        """
        self.out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = self.out_dir / f"{stamp}-agent{metadata.get('agent_id', 'x')}-{key[:8]}.md"

        params = kwargs.get("invocation_params") or {}
        tools = params.get("tools") or []

        header = [
            "# Agent prompt dump\n",
            f"- captured: {datetime.now(UTC).isoformat()}",
            f"- agent_id: {metadata.get('agent_id')}",
            f"- session_id: {key}",
            f"- model: {params.get('model') or params.get('model_name') or 'unknown'}",
            f"- tools bound: {len(tools)}\n",
            "The system message below is re-sent unchanged on every iteration.",
            "What grows between iterations is the tail: each tool call and its",
            "result is appended before the model is asked again.\n",
        ]
        if tools:
            header.append(f"\n## Tools bound ({len(tools)})\n")
            header.append(_render_tools(tools))

        path.write_text("\n".join(header), encoding="utf-8")
        logger.info("[PROMPT-DUMP] writing to %s", path)
        return path


def get_prompt_dump_callbacks() -> list:
    """Return ``[PromptDumpCallback]`` when DUMP_AGENT_PROMPT is set, else ``[]``.

    Mirrors get_langsmith_callbacks' shape so the two compose by concatenation
    at the call site.
    """
    if os.getenv("DUMP_AGENT_PROMPT", "").lower() not in {"1", "true", "yes"}:
        return []
    return [PromptDumpCallback()]
