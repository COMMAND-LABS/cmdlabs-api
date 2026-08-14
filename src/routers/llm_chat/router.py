"""
Direct LLM completions (SSE) — the model without an agent around it.

WHAT THIS IS NOT
----------------
Not the agent runtime. No agent config, no tools, no tool entitlement, no HITL
approvals, and — deliberately — NO PERSISTENCE. The client holds the transcript
and sends it whole on every turn, so there is no chat_sessions row and nothing
to clean up. That keeps this endpoint a pure function of its request: caller +
credentials + messages in, token stream out. If saved LLM conversations ever
matter, that is a feature to design (it would need its own session surface),
not a column to sneak in here.

WHOSE KEY FUNDS IT
------------------
Always the caller's. Agents have an owner and a shareOwnerCredentials flag;
here there is no agent, so there is no other principal whose key could apply.
The caller's default credential for the chosen provider is resolved per
request, exactly like the agent runtime does for a caller running on their own
credentials.

GATING
------
Mounted under /api/llm-chat, which modules_registry maps to the `llm_chat`
module — premium-only in plans_registry. The route-level require_module
dependency in main.py is the enforcement; nothing here re-checks the plan.

SSE CONTRACT
------------
Same frames the agent stream emits for a plain chat turn —
on_chat_model_start, on_chat_model_stream, on_chain_end, error — so the UI's
parser is shared, not forked.
"""
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from src.agent_runtime.helpers import sse_error, sse_event
from src.agent_runtime.helpers.llm_factory import (
    create_llm,
    get_required_credential_type,
)
from src.deps import auth_dependency, db_dependency
from src.rate_limit import limiter
from src.routers.credentials.encryption import get_credential_value
from src.services.credential_access import resolve_default_credential
from src.utils.langsmith import get_langsmith_callbacks

logger = logging.getLogger(__name__)

router = APIRouter()
callbacks = get_langsmith_callbacks("llm-chat")

SUPPORTED_PROVIDERS = ("openai", "anthropic", "google", "kimi", "ollama")

# The transcript rides in each request, so its size is the request's cost.
# Bound it so a runaway client cannot ship an unbounded prompt; a long
# conversation degrades by dropping its oldest turns client-side.
MAX_HISTORY_MESSAGES = 200


class HistoryMessage(BaseModel):
    # The UI's Message roles. tool_approval never appears here — that is an
    # agent-runtime concept and this endpoint has no tools.
    role: str
    content: str = Field(max_length=200_000)

    @field_validator("role")
    @classmethod
    def _known_role(cls, v: str) -> str:
        if v not in ("human", "ai"):
            raise ValueError("role must be 'human' or 'ai'")
        return v


class LlmChatPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    history: list[HistoryMessage] = Field(default_factory=list,
                                          max_length=MAX_HISTORY_MESSAGES)
    provider: str
    model: str = Field(min_length=1, max_length=128)
    systemPrompt: str | None = Field(default=None, max_length=100_000)
    temperature: float = Field(default=0, ge=0, le=1)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in SUPPORTED_PROVIDERS:
            raise ValueError(
                f"provider must be one of {SUPPORTED_PROVIDERS}")
        return v


async def _generator(body: LlmChatPrompt, db, auth: dict):
    """Setup failures become SSE error frames, not HTTP errors — by the time
    a browser is reading this stream the response status has already gone out,
    and the chat UI renders error frames in-line where the reply would be."""
    account_id = auth["id"]

    # --- Credential (always the caller's own) ---
    credentials: dict[str, str] = {}
    required_credential_type = get_required_credential_type(body.provider)
    if required_credential_type:
        credential = resolve_default_credential(
            db, account_id, required_credential_type)
        if not credential:
            yield sse_error(
                f"{body.provider.title()} API key required",
                f"Please add your {body.provider.title()} API key in account "
                f"settings to use {body.model}.",
            )
            return
        try:
            credentials[body.provider] = get_credential_value(
                credential, "api_key")
        except Exception as exc:
            logger.exception("[LLM-CHAT] credential decryption failed")
            yield sse_error("Failed to retrieve API key", str(exc))
            return

    # --- LLM ---
    try:
        llm, _ = create_llm(
            model_config={"provider": body.provider, "model": body.model},
            credentials=credentials,
            temperature=body.temperature,
        )
    except ValueError as exc:
        yield sse_error("LLM initialization failed", str(exc))
        return

    # --- Messages: optional system + client-held history + this turn ---
    messages: list[tuple[str, str]] = []
    if body.systemPrompt and body.systemPrompt.strip():
        messages.append(("system", body.systemPrompt))
    messages.extend((m.role, m.content) for m in body.history)
    messages.append(("human", body.prompt))

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
                    full_response += (
                        content if isinstance(content, str)
                        else "".join(
                            block.get("text", "")
                            for block in content
                            if isinstance(block, dict)
                            and block.get("type") == "text"
                        )
                    )
                    yield sse_event("on_chat_model_stream", data=content)
    except Exception as exc:
        logger.exception("[LLM-CHAT] streaming error")
        yield sse_error("Streaming error", str(exc))
        return

    yield sse_event("on_chain_end", data=full_response)


@router.post("/stream")
@limiter.limit("200/minute")
async def llm_completion(
    request_body: LlmChatPrompt,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    """Stream a direct LLM completion over the caller's own provider key."""
    return StreamingResponse(
        _generator(request_body, db, auth),
        media_type="text/event-stream",
    )
