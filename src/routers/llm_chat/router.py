"""
Direct LLM completions (SSE) — the model without an agent around it.

WHAT THIS IS NOT
----------------
Not the agent runtime. No agent config, no tools, no tool entitlement, no HITL
approvals, and — deliberately — NO PERSISTENCE and NO MEMORY. Each request is
ONE completion: optional system prompt + the current prompt, nothing else. A
`history` field used to ride along here, which quietly made "the raw model"
surface a conversational one; it is gone on purpose. This is the teaching
contrast to Memory Chat (server-held transcript + context window): here every
turn starts blank. That keeps this endpoint a pure function of its request:
caller + credentials + one prompt in, token stream out. If saved LLM
conversations ever matter, that is a feature to design (it would need its own
session surface), not a field to sneak back in here.

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
from contextlib import aclosing

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from src.agent_runtime.helpers.direct_chat import (
    caller_llm_or_sse_error,
    check_known_provider,
    stream_completion,
)
from src.deps import auth_dependency, db_dependency
from src.rate_limit import limiter
from src.utils.langsmith import get_langsmith_callbacks

logger = logging.getLogger(__name__)

router = APIRouter()
callbacks = get_langsmith_callbacks("llm-chat")


class LlmChatPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    provider: str
    model: str = Field(min_length=1, max_length=128)
    systemPrompt: str | None = Field(default=None, max_length=100_000)
    temperature: float = Field(default=0, ge=0, le=1)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        return check_known_provider(v)


async def _generator(body: LlmChatPrompt, db, auth: dict):
    """Setup failures become SSE error frames, not HTTP errors — by the time
    a browser is reading this stream the response status has already gone out,
    and the chat UI renders error frames in-line where the reply would be."""
    account_id = auth["id"]

    # --- Credential (always the caller's own) + LLM ---
    llm, error = caller_llm_or_sse_error(
        db, account_id, body.provider, body.model, body.temperature,
        logger=logger, log_tag="LLM-CHAT",
    )
    if error:
        yield error
        return

    # --- Messages: optional system + this turn. Nothing else, by contract:
    # every request is a fresh single completion with no memory. ---
    messages: list[tuple[str, str]] = []
    if body.systemPrompt and body.systemPrompt.strip():
        messages.append(("system", body.systemPrompt))
    messages.append(("human", body.prompt))

    async with aclosing(stream_completion(
        llm, messages, callbacks=callbacks, logger=logger, log_tag="LLM-CHAT",
    )) as frames:
        async for frame in frames:
            yield frame


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
