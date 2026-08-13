"""
Agent completion helpers.

This module contains refactored helper functions for the agent completion endpoint.
"""
from .auth import extract_auth_token
from .llm_factory import (
    DEFAULT_MODEL_CONFIG,
    create_llm,
    get_model_config,
    get_required_credential_type,
)
from .message_history import (
    build_message_history,
    store_ai_message,
    store_user_message,
)
from .sse_events import EventType, sse_error, sse_event
from .tool_calls import format_tool_call

__all__ = [
    "DEFAULT_MODEL_CONFIG",
    "EventType",
    # Message history
    "build_message_history",
    "create_llm",
    # Auth
    "extract_auth_token",
    # Tool calls
    "format_tool_call",
    # LLM factory
    "get_model_config",
    "get_required_credential_type",
    "sse_error",
    # SSE events
    "sse_event",
    "store_ai_message",
    "store_user_message",
]
