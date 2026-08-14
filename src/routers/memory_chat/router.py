"""
Memory Chat — the context-window teaching demo (SSE completions + persistence).

WHAT THIS TEACHES
-----------------
Two things at once, and the split is the lesson:

  PERSISTENCE   every prompt and completion is a memory_chat_messages row, so
                the transcript survives client restarts. One rolling
                conversation per (account, org); no session objects.
  THE WINDOW    the model is only ever sent the newest turns that fit inside
                HALF the chosen context limit. Older turns are "dropped" —
                excluded from the model's input — but their rows remain, so
                the UI can show what the model no longer remembers.

The context limits offered are deliberately TOY-SIZED (2K–32K tokens).
A real model's window is 200K+ and nobody types that into a demo; a small
window lets the reader watch messages fall out within a few minutes. Token
counts are the same ~4-characters-per-token estimate the courseware teaches —
close enough to see the mechanics, not a billing meter.

WHOSE KEY FUNDS IT
------------------
The caller's own, resolved per request — same as llm-chat, and for the same
reason: there is no agent here, so no other principal's credential can apply.

SSE CONTRACT
------------
Same frames as /api/llm-chat/stream (on_chat_model_start,
on_chat_model_stream, on_chain_end, error), so the UI's parser is shared.
After the stream ends the client refetches GET / — the server is the source
of truth for ids and window membership.
"""
import logging
import math

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from src.agent_runtime.helpers import sse_error, sse_event
from src.agent_runtime.helpers.llm_factory import (
    create_llm,
    get_required_credential_type,
)
from src.db.models import MemoryChatMessage
from src.deps import db_dependency, jwt_dependency, org_dependency, account_id_from_claims
from src.rate_limit import limiter
from src.routers.credentials.encryption import get_credential_value
from src.services.credential_access import resolve_default_credential
from src.utils.langsmith import get_langsmith_callbacks

logger = logging.getLogger(__name__)

router = APIRouter()
callbacks = get_langsmith_callbacks("memory-chat")

SUPPORTED_PROVIDERS = ("openai", "anthropic", "google", "kimi", "ollama")

# Toy context limits (total tokens). The model's usable budget is HALF of the
# chosen limit — "once half of the context limit is reached, earlier content
# is dropped".
ALLOWED_CONTEXT_LIMITS = (2_000, 4_000, 8_000, 16_000, 32_000)
DEFAULT_CONTEXT_LIMIT = 4_000

SYSTEM_PROMPT = (
    "You are the model inside a memory demo. Answer conversationally and "
    "concisely. You only see the recent portion of the conversation; if the "
    "user references something you have no record of, say plainly that it "
    "has fallen outside your context window."
)


def estimate_tokens(text: str) -> int:
    """~4 characters per token — the same rule of thumb the courseware
    teaches. An estimate on purpose: the demo shows mechanics, not billing."""
    return max(1, math.ceil(len(text) / 4))


def compute_window(messages: list, context_limit: int) -> int:
    """The index of the OLDEST message still inside the model's window.

    Walks newest → oldest accumulating estimated tokens until the budget
    (half the context limit) is spent. Returns len(messages) when nothing
    fits — the newest message alone can exceed a toy budget.
    """
    budget = context_limit // 2
    used = 0
    start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        tokens = estimate_tokens(messages[i].content)
        if used + tokens > budget:
            break
        used += tokens
        start = i
    return start


def _context_limit_or_default(value: int | None) -> int:
    return value if value in ALLOWED_CONTEXT_LIMITS else DEFAULT_CONTEXT_LIMIT


def _load_messages(db, account_id: int, org_id: int) -> list:
    return (
        db.query(MemoryChatMessage)
        .filter(
            MemoryChatMessage.account_id == account_id,
            MemoryChatMessage.org_id == org_id,
        )
        .order_by(MemoryChatMessage.id)
        .all()
    )


# ── GET: the transcript, annotated with window membership ────────────────────

class MessageOut(BaseModel):
    id: int
    role: str
    content: str
    tokens: int
    in_window: bool
    created_at: str


class WindowOut(BaseModel):
    context_limit: int
    budget: int
    used_tokens: int
    dropped_count: int


class TranscriptResponse(BaseModel):
    messages: list[MessageOut]
    window: WindowOut


@router.get("/", response_model=TranscriptResponse)
@limiter.limit("60/minute")
async def get_transcript(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
    context_limit: int | None = Query(default=None),
):
    """The stored conversation, with each message's token estimate and
    whether it is still inside the model's window at the given limit."""
    account_id = account_id_from_claims(jwt)
    messages = _load_messages(db, account_id, org.org_id)

    limit = _context_limit_or_default(context_limit)
    start = compute_window(messages, limit)
    used = sum(estimate_tokens(m.content) for m in messages[start:])

    return TranscriptResponse(
        messages=[
            MessageOut(
                id=m.id,
                role=m.role,
                content=m.content,
                tokens=estimate_tokens(m.content),
                in_window=i >= start,
                created_at=m.created_at.isoformat() if m.created_at else "",
            )
            for i, m in enumerate(messages)
        ],
        window=WindowOut(
            context_limit=limit,
            budget=limit // 2,
            used_tokens=used,
            dropped_count=start,
        ),
    )


# ── POST: one turn — persist prompt, stream completion, persist completion ──

class MemoryChatPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    provider: str
    model: str = Field(min_length=1, max_length=128)
    context_limit: int = Field(default=DEFAULT_CONTEXT_LIMIT)
    temperature: float = Field(default=0, ge=0, le=1)

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str) -> str:
        if v not in SUPPORTED_PROVIDERS:
            raise ValueError(f"provider must be one of {SUPPORTED_PROVIDERS}")
        return v

    @field_validator("context_limit")
    @classmethod
    def _known_limit(cls, v: int) -> int:
        if v not in ALLOWED_CONTEXT_LIMITS:
            raise ValueError(
                f"context_limit must be one of {ALLOWED_CONTEXT_LIMITS}")
        return v


async def _generator(body: MemoryChatPrompt, db, account_id: int, org_id: int):
    """Setup failures become SSE error frames, not HTTP errors — by the time
    a browser reads this stream the status has already gone out (same contract
    as llm-chat).

    The PROMPT IS PERSISTED FIRST, before anything can fail: "as each prompt
    is sent, it is added to the session". A turn whose completion errored
    still happened; the reader sees their message survive a reload either way.
    """
    human = MemoryChatMessage(
        account_id=account_id, org_id=org_id,
        role="human", content=body.prompt,
    )
    db.add(human)
    db.commit()

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
            logger.exception("[MEMORY-CHAT] credential decryption failed")
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

    # --- The window: newest turns that fit in half the context limit ---
    stored = _load_messages(db, account_id, org_id)
    start = compute_window(stored, body.context_limit)
    windowed = stored[start:]

    messages: list[tuple[str, str]] = [("system", SYSTEM_PROMPT)]
    messages.extend((m.role, m.content) for m in windowed)

    yield sse_event("on_chat_model_start")
    full_response = ""
    config = {"callbacks": callbacks} if callbacks else {}

    try:
        async for event in llm.astream_events(messages, version="v1",
                                              config=config):
            if event["event"] == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
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
        logger.exception("[MEMORY-CHAT] streaming error")
        yield sse_error("Streaming error", str(exc))
        return

    # --- Persist the completion ---
    if full_response:
        db.add(MemoryChatMessage(
            account_id=account_id, org_id=org_id,
            role="ai", content=full_response,
        ))
        db.commit()

    yield sse_event("on_chain_end", data=full_response)


@router.post("/stream")
@limiter.limit("60/minute")
async def memory_chat_completion(
    request_body: MemoryChatPrompt,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """One demo turn: persist the prompt, stream a completion over the
    windowed history, persist the completion."""
    account_id = account_id_from_claims(jwt)
    return StreamingResponse(
        _generator(request_body, db, account_id, org.org_id),
        media_type="text/event-stream",
    )


# ── DELETE: reset the demo ───────────────────────────────────────────────────

@router.delete("/", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def clear_transcript(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Delete the caller's whole demo conversation — the only path that ever
    removes rows. The window never deletes; this does."""
    account_id = account_id_from_claims(jwt)
    db.query(MemoryChatMessage).filter(
        MemoryChatMessage.account_id == account_id,
        MemoryChatMessage.org_id == org.org_id,
    ).delete()
    db.commit()
