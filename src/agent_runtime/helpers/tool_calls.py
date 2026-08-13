"""
Tool call formatting helpers for agent completion.

Handles formatting tool call data according to the chat_message.v2.json schema.
"""
import ast
import json as _json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def format_tool_call(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: Any
) -> dict[str, Any] | None:
    """
    Format a tool call according to the chat_message.v2.json schema.

    Determines the tool type from the tool name and formats the input/output
    appropriately for each tool type.

    Args:
        tool_name: The name of the tool that was executed
        tool_input: The input that was passed to the tool
        tool_output: The output returned by the tool

    Returns:
        Formatted tool call dict, or None if the tool output is invalid
    """
    # Normalize tool_output to a dict.
    # LangChain's StructuredTool.arun() calls str() on the tool's return value before
    # passing it to the on_tool_end callback, so tool_output is typically a Python repr
    # string of the original dict (e.g. "{'results': [...], 'namespace': '...'}").
    # Newer LangChain versions may also pass a ToolMessage/AIMessage object — extract
    # the string content from those before attempting to parse.
    if not isinstance(tool_output, dict):
        # Unwrap LangChain message objects (ToolMessage, AIMessage, etc.)
        if hasattr(tool_output, "content"):
            tool_output = tool_output.content

        if isinstance(tool_output, str) and tool_output.strip():
            # Try JSON first (clean format), then Python literal eval (str() format)
            try:
                parsed = _json.loads(tool_output)
                tool_output = parsed if isinstance(parsed, dict) else {"result": tool_output}
            except (_json.JSONDecodeError, ValueError):
                try:
                    parsed = ast.literal_eval(tool_output)
                    tool_output = parsed if isinstance(parsed, dict) else {"result": tool_output}
                except (ValueError, SyntaxError):
                    logger.debug("[TOOL CALLS] Could not parse tool_output string; wrapping as result")
                    tool_output = {"result": tool_output}
        else:
            logger.debug(f"[TOOL CALLS] Normalizing non-dict/non-str tool_output (type: {type(tool_output).__name__})")
            tool_output = {"result": str(tool_output)}

    _FORMATTERS = {
        "vector_search": lambda n, i, o: _format_vector_search(n, i, o, "vectorSearch"),
        "vector_search_with_reranking": lambda n, i, o: _format_vector_search(n, i, o, "vectorSearchWithReranking"),
        "send_txt_email_with_ses": _format_send_txt_email,
        "send_html_email_with_ses": _format_send_html_email,
    }

    formatter = _FORMATTERS.get(tool_name)
    if formatter is None:
        if tool_name.startswith("db_table_read"):
            formatter = _format_db_table_read
        elif tool_name.startswith("db_table_write"):
            formatter = _format_db_table_write
        else:
            formatter = _format_generic_tool
    return formatter(tool_name, tool_input, tool_output)


def _format_vector_search(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
    tool_type: str = "vectorSearch",
) -> dict[str, Any] | None:
    """Format vector search tool call. Returns None for error outputs."""
    if 'error' in tool_output and 'results' not in tool_output:
        return None

    results = _format_search_results(tool_output.get('results', []))

    input_data: dict[str, Any] = {"query": tool_input.get('query', '')}
    top_k = tool_input.get('top_k', tool_input.get('topK'))
    if top_k is not None:
        input_data["topK"] = int(top_k)

    return {
        "toolType": tool_type,
        "toolName": tool_name,
        "input": input_data,
        "output": {
            "results": results,
            "namespace": tool_output.get('namespace', ''),
            "index": tool_output.get('index', '')
        }
    }


def _format_search_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Format search results according to v2 schema."""
    return [
        {
            "id": result.get("id", ""),
            "score": result.get("score", 0.0),
            "metadata": result.get("metadata", {}),
        }
        for result in results
    ]


def _format_db_table_read(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any]
) -> dict[str, Any]:
    """Format database table read tool call."""
    return {
        "toolType": "dbTableRead",
        "toolName": tool_name,
        "input": {
            "filters": tool_input.get('filters'),
            "limit": tool_input.get('limit'),
            "offset": tool_input.get('offset')
        },
        "output": {
            "results": tool_output.get('results', []),
            "table": tool_output.get('table', ''),
            "count": tool_output.get('count', 0)
        }
    }


def _format_db_table_write(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any]
) -> dict[str, Any]:
    """Format database table write tool call."""
    return {
        "toolType": "dbTableWrite",
        "toolName": tool_name,
        "input": {
            "data": tool_input  # The flat input IS the data
        },
        "output": {
            "success": tool_output.get('success', False),
            "table": tool_output.get('table', ''),
            "inserted": tool_output.get('inserted', {}),
            "message": tool_output.get('message', ''),
            "error": tool_output.get('error')
        }
    }


def _format_send_txt_email(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
) -> dict[str, Any]:
    """Format a send-plain-text-email tool call."""
    return {
        "toolType": "sendTxtEmailWithSes",
        "toolName": tool_name,
        "input": {
            "to": tool_input.get("to_email", tool_input.get("to", "")),
            "subject": tool_input.get("subject", ""),
            "body": tool_input.get("body", ""),
        },
        "output": {
            "success": tool_output.get("success", False),
            "messageId": tool_output.get("message_id", tool_output.get("messageId")),
            "error": tool_output.get("error"),
        },
    }


def _format_send_html_email(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any],
) -> dict[str, Any]:
    """Format a send-HTML-email tool call (template or raw-HTML mode)."""
    inp: dict[str, Any] = {
        "to": tool_input.get("to_email", tool_input.get("to", "")),
        "subject": tool_input.get("subject", ""),
        "html_body": tool_input.get("html_body") or None,
    }
    # Include template metadata when present
    if tool_input.get("template_id") is not None:
        inp["template_id"] = tool_input["template_id"]
    if tool_input.get("variables"):
        inp["variables"] = tool_input["variables"]
    return {
        "toolType": "sendHtmlEmailWithSes",
        "toolName": tool_name,
        "input": inp,
        "output": {
            "success": tool_output.get("success", False),
            "messageId": tool_output.get("message_id", tool_output.get("messageId")),
            "error": tool_output.get("error"),
        },
    }


def _format_generic_tool(
    tool_name: str,
    tool_input: dict[str, Any],
    tool_output: dict[str, Any]
) -> dict[str, Any]:
    """Format generic/unknown tool call."""
    return {
        "toolType": "custom",
        "toolName": tool_name,
        "input": tool_input,
        "output": tool_output
    }
