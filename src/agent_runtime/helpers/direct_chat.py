"""
Shared plumbing for the agent-less SSE completion endpoints (llm-chat and
memory-chat): the provider allow-list, caller-LLM setup mapped to SSE error
frames, and the astream_events loop that emits the shared frame contract
(on_chat_model_start, on_chat_model_stream, on_chain_end, error).
"""
import logging
from collections.abc import AsyncIterator, Callable

from langchain_core.language_models.chat_models import BaseChatModel

from .caller_llm import (
    CredentialDecryptError,
    LlmInitError,
    MissingCredentialError,
    create_caller_llm,
)
from .sse_events import sse_error, sse_event

SUPPORTED_PROVIDERS = ("openai", "anthropic", "google", "kimi", "ollama")


def check_known_provider(v: str) -> str:
    """Body for the request models' ``provider`` field_validator."""
    if v not in SUPPORTED_PROVIDERS:
        raise ValueError(f"provider must be one of {SUPPORTED_PROVIDERS}")
    return v


def caller_llm_or_sse_error(
    db,
    account_id: int,
    provider: str,
    model: str,
    temperature: float,
    *,
    logger: logging.Logger,
    log_tag: str,
) -> tuple[BaseChatModel | None, str | None]:
    """The caller's LLM, or the SSE error frame to yield instead.

    Returns ``(llm, None)`` on success, ``(None, frame)`` on a setup failure.
    ``logger``/``log_tag`` are the calling router's, so the decryption-failure
    log line keeps its logger name and "[TAG]" prefix.
    """
    try:
        return create_caller_llm(
            db, account_id, provider, model, temperature=temperature), None
    except MissingCredentialError:
        return None, sse_error(
            f"{provider.title()} API key required",
            f"Please add your {provider.title()} API key in account "
            f"settings to use {model}.",
        )
    except CredentialDecryptError as exc:
        logger.exception(f"[{log_tag}] credential decryption failed")
        return None, sse_error("Failed to retrieve API key", str(exc))
    except LlmInitError as exc:
        return None, sse_error("LLM initialization failed", str(exc))


def _chunk_text(content) -> str:
    """Plain text from a stream chunk's content: strings pass through,
    content-block lists keep only their text blocks."""
    return (
        content if isinstance(content, str)
        else "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
        )
    )


async def stream_completion(
    llm: BaseChatModel,
    messages: list[tuple[str, str]],
    *,
    callbacks: list,
    logger: logging.Logger,
    log_tag: str,
    on_complete: Callable[[str], None] | None = None,
) -> AsyncIterator[str]:
    """Stream one completion as SSE frames.

    Emits on_chat_model_start, one on_chat_model_stream per non-empty chunk,
    then on_chain_end with the accumulated text. A streaming failure emits a
    single "Streaming error" frame and stops (no on_chain_end, no
    ``on_complete``). ``on_complete`` runs with the full response just before
    on_chain_end.
    """
    yield sse_event("on_chat_model_start")
    full_response = ""
    config = {"callbacks": callbacks} if callbacks else {}

    try:
        async for event in llm.astream_events(messages, version="v1",
                                              config=config):
            if event["event"] == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    # Anthropic streams content-block lists, OpenAI plain
                    # strings; the UI's extractTextContent handles both, so
                    # forward verbatim — same as the agent stream.
                    full_response += _chunk_text(content)
                    yield sse_event("on_chat_model_stream", data=content)
    except Exception as exc:
        logger.exception(f"[{log_tag}] streaming error")
        yield sse_error("Streaming error", str(exc))
        return

    if on_complete is not None:
        on_complete(full_response)

    yield sse_event("on_chain_end", data=full_response)
