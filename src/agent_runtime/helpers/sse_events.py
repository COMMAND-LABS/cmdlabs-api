"""
Server-Sent Events (SSE) helpers for agent completion.

Provides consistent formatting for SSE events sent to the client.

Each event is emitted as a standard SSE frame: a single ``data:`` line holding
the compact JSON payload, terminated by a blank line (``\\n\\n``). This lets
clients use off-the-shelf SSE parsers instead of hand-rolled JSON-boundary
scanning. The ``event`` type stays inside the JSON payload (not the SSE
``event:`` line) so the client reads it uniformly from the parsed object.
"""
import json
from typing import Any


def _frame(payload: dict[str, Any]) -> str:
    """Wrap a payload dict as a standard SSE ``data:`` frame."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def sse_event(
    event: str,
    data: Any | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
) -> str:
    """
    Create a standard SSE event frame (``data: <json>\\n\\n``).

    Args:
        event: The event type (e.g., "on_chain_start", "on_chat_model_stream")
        data: Optional data payload for the event
        tool_calls: Optional list of tool calls to include (used only on on_chain_end)
        run_id: Optional LangChain run ID for correlating on_tool_start / on_tool_end pairs

    Returns:
        SSE frame string ready to be yielded in a StreamingResponse
    """
    payload: dict[str, Any] = {"event": event}

    if data is not None:
        payload["data"] = data

    if run_id:
        payload["run_id"] = run_id

    if tool_calls:  # only include when non-empty
        payload["toolCalls"] = tool_calls

    return _frame(payload)


def sse_error(error: str, message: str) -> str:
    """
    Create a standard SSE error event frame (``data: <json>\\n\\n``).

    Args:
        error: Short error type/code
        message: Human-readable error message

    Returns:
        SSE frame string for an error event
    """
    return _frame({
        "event": "error",
        "data": {
            "error": error,
            "message": message
        }
    })


# Common event types as constants for consistency
class EventType:
    """SSE event type constants."""
    CHAIN_START = "on_chain_start"
    CHAIN_END = "on_chain_end"
    CHAT_MODEL_START = "on_chat_model_start"
    CHAT_MODEL_STREAM = "on_chat_model_stream"
    CHAT_MODEL_END = "on_chat_model_end"
    TOOL_START = "on_tool_start"
    TOOL_END = "on_tool_end"
    ERROR = "error"
    # Emitted when a HITL-gated tool queues an action for human review.
    # Payload: {approval_id, tool_type, preview: {to_email, subject, body}}
    TOOL_APPROVAL_REQUIRED = "tool_approval_required"
