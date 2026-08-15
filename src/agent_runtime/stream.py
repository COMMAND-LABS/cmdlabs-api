"""Agent streaming completion endpoint (SSE)."""

import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.core.schemas.ChatSessionPrompt import ChatSessionPrompt
from src.deps import auth_dependency, db_dependency
from src.rate_limit import limiter
from src.agent_runtime.context import (
    AgentContext,
    AgentSetupError,
    persist_ai_message,
    persist_user_message,
    prepare_agent_context,
)
from src.agent_runtime.helpers import (
    EventType,
    format_tool_call,
    sse_error,
    sse_event,
)
from src.agent_runtime.prompt_dump import get_prompt_dump_callbacks
from src.utils.langsmith import get_langsmith_callbacks

logger = logging.getLogger(__name__)

router = APIRouter()
# The dump handler is a no-op unless DUMP_AGENT_PROMPT is set, so this costs
# nothing in normal operation.
callbacks = get_langsmith_callbacks("dynamic-agent") + get_prompt_dump_callbacks()


async def generator(
    agent_id: int | None = None,
    session_id: str | None = None,
    prompt: str | None = None,
    db=None,
    auth: dict | None = None,
    request: Request | None = None,
    pdf_base64: str | None = None,
    pdf_filename: str | None = None,
    pdf_use_vision: bool = False,
    image_base64: str | None = None,
    document_text: str | None = None,
    attachment_filename: str | None = None,
    attachment_content_type: str | None = None,
    gcs_bucket: str | None = None,
    gcs_file_path: str | None = None,
    agent_config_override: dict | None = None,
):
    try:
        ctx = await prepare_agent_context(
            agent_id=agent_id,
            session_id=session_id,
            prompt=prompt,
            db=db,
            auth=auth,
            request=request,
            callbacks=callbacks,
            pdf_base64=pdf_base64,
            pdf_filename=pdf_filename,
            pdf_use_vision=pdf_use_vision,
            image_base64=image_base64,
            document_text=document_text,
            attachment_filename=attachment_filename,
            attachment_content_type=attachment_content_type,
            gcs_bucket=gcs_bucket,
            gcs_file_path=gcs_file_path,
            agent_config_override=agent_config_override,
        )
    except AgentSetupError as exc:
        yield sse_error(exc.title, exc.detail)
        return

    try:
        if ctx.agent_executor:
            async for event_data in _stream_agent_executor(ctx):
                yield event_data
        else:
            async for event_data in _stream_simple_chat(ctx):
                yield event_data
    except Exception as e:
        logger.exception(f"[STREAM] Unhandled error during agent stream: {e!s}")
        yield sse_error("Internal server error", str(e))


def _extract_text(content) -> str:
    """Plain text from a stream chunk: OpenAI sends strings, Anthropic sends
    content-block lists. Mirrors the UI's extractTextContent."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


async def _stream_agent_executor(ctx: AgentContext):
    user_message_stored = False
    tool_calls = []

    # A multi-step turn is several model calls with tool calls in between.
    # AgentExecutor's final output is only the LAST model response, so
    # persisting that (as this used to) silently drops every earlier segment
    # — text the user already watched stream. Track the turn ourselves:
    # `segments` holds each model call's text; `blocks` records the
    # presentation order (text segments interleaved with toolCalls indices).
    segments: list[str] = []
    blocks: list[dict] = []

    async for event in ctx.agent_executor.astream_events(
        {"input": ctx.agent_input},
        version="v1",
    ):
        kind = event["event"]

        if kind == "on_chain_start":
            if event["name"] == "Agent":
                yield sse_event("on_chain_start")

        elif kind == "on_chain_end":
            if event["name"] == "Agent":
                # Materialize the turn: drop empty segments, join the rest.
                final_blocks: list[dict] = []
                for block in blocks:
                    if block["kind"] == "text":
                        text = segments[block["segment"]].strip()
                        if text:
                            final_blocks.append({"kind": "text", "content": text})
                    else:
                        final_blocks.append(block)
                content = "\n\n".join(
                    b["content"] for b in final_blocks if b["kind"] == "text"
                )
                if not content:
                    # No streamed text captured (unexpected provider shape) —
                    # fall back to the executor's final output as before.
                    content = event["data"].get("output", {}).get("output", "")
                    final_blocks = []
                # blocks only earn their keep when there is an order to keep:
                # a single text segment renders from `content` alone.
                interleaved = final_blocks if len(final_blocks) > 1 else None
                persist_ai_message(ctx.chat_session_id, content,
                                   tool_calls if tool_calls else None,
                                   blocks=interleaved)
                yield sse_event("on_chain_end", data=content,
                                tool_calls=tool_calls if tool_calls else None,
                                blocks=interleaved)

        elif kind == "on_chat_model_start":
            if not user_message_stored:
                persist_user_message(ctx.chat_session_id, ctx.prompt, ctx.pdf_filename, ctx.attachment_ref)
                user_message_stored = True
            # Each model call opens a new text segment — this event firing
            # again after tool activity IS the segment boundary.
            segments.append("")
            blocks.append({"kind": "text", "segment": len(segments) - 1})
            yield sse_event("on_chat_model_start", tool_calls=tool_calls)

        elif kind == "on_chat_model_stream":
            content = event["data"]["chunk"].content
            if content:
                if segments:
                    segments[-1] += _extract_text(content)
                yield sse_event("on_chat_model_stream", data=content)

        elif kind == "on_tool_start":
            run_id = event.get("run_id", "")
            tool_input = _parse_tool_input(event["data"].get("input", {}))
            yield sse_event("on_tool_start", data={
                "name": event["name"],
                "input": tool_input if isinstance(tool_input, dict) else {},
            }, run_id=run_id)

        elif kind == "on_tool_end":
            run_id = event.get("run_id", "")
            tool_output = event["data"].get("output", {})

            hitl_data = _detect_hitl_sentinel(tool_output)
            if hitl_data:
                yield sse_event(
                    EventType.TOOL_APPROVAL_REQUIRED,
                    data={
                        "approval_id": hitl_data.get("approval_id"),
                        "tool_type": hitl_data.get("tool_type"),
                        "preview": hitl_data.get("preview", {}),
                    },
                )
                yield sse_event("on_tool_end", run_id=run_id)
            else:
                tool_input = _parse_tool_input(event["data"].get("input", {}))
                formatted = format_tool_call(
                    tool_name=event["name"],
                    tool_input=tool_input if isinstance(tool_input, dict) else {},
                    tool_output=tool_output,
                )
                if formatted:
                    tool_calls.append(formatted)
                    blocks.append({"kind": "tool", "index": len(tool_calls) - 1})
                yield sse_event("on_tool_end", data=formatted, run_id=run_id)


async def _stream_simple_chat(ctx: AgentContext):
    persist_user_message(ctx.chat_session_id, ctx.prompt, ctx.pdf_filename, ctx.attachment_ref)
    yield sse_event("on_chat_model_start")
    full_response = ""

    try:
        if isinstance(ctx.agent_input, str):
            formatted_input = ctx.prompt_template.format_messages(
                chat_history=ctx.message_history.messages,
                input=ctx.agent_input,
            )
        else:
            messages = [
                ("system", ctx.prompt_template.messages[0].prompt.template),
            ]
            messages.extend(ctx.message_history.messages)
            messages.append(ctx.agent_input)
            formatted_input = messages

        config = {"callbacks": ctx.callbacks} if ctx.callbacks else {}

        async for event in ctx.llm.astream_events(
            formatted_input,
            version="v1",
            config=config,
        ):
            if event["event"] == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    full_response += content
                    yield sse_event("on_chat_model_stream", data=content)

    except Exception as e:
        logger.exception(f"[STREAM] Error during streaming: {e!s}")
        yield sse_error("Streaming error", str(e))
        return

    if full_response:
        persist_ai_message(ctx.chat_session_id, full_response)
    yield sse_event("on_chain_end", data=full_response)


def _parse_tool_input(raw):
    """Normalize tool input that LangChain may pass as a string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            try:
                import ast
                return ast.literal_eval(raw)
            except Exception:
                return {}
    return {}


def _detect_hitl_sentinel(tool_output) -> dict | None:
    """Return parsed HITL data if tool_output contains the approval sentinel."""
    if isinstance(tool_output, str):
        try:
            parsed = json.loads(tool_output)
            if parsed.get("__approval_required__"):
                return parsed
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
    return None


@router.post("/{agent_id}/stream")
@limiter.limit("200/minute")
async def agent_completion(
    agent_id: int,
    request_body: ChatSessionPrompt,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    """Stream completion from a dynamically configured agent."""
    return StreamingResponse(
        generator(
            agent_id=agent_id,
            session_id=request_body.sessionId,
            prompt=request_body.prompt,
            db=db,
            auth=auth,
            request=request,
            pdf_base64=request_body.pdf,
            pdf_filename=request_body.pdfFilename,
            pdf_use_vision=request_body.pdfUseVision or False,
            image_base64=request_body.image,
            document_text=request_body.documentText,
            attachment_filename=request_body.attachmentFilename,
            attachment_content_type=request_body.attachmentContentType,
            gcs_bucket=request_body.gcsBucket,
            gcs_file_path=request_body.gcsFilePath,
        ),
        media_type="text/event-stream",
    )
