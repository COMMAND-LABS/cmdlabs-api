"""Dedicated contact-scoped chat endpoint (SSE).

A separate, narrow path so the code-defined contact agent never touches the
generic agent flow or its per-account access checks. It reuses the exact same
SSE `generator` as the normal agent stream — only the agent config differs
(injected, code-defined) and there is no agent_id.

Authorization model: the path session_id is resolved by
prepare_agent_context with `ChatSession.account_id == caller`, and the
session's contact binding (validated at session creation) plus the
structurally-scoped tools enforce that only this contact's data is reachable.
"""

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from src.core.schemas.ChatSessionPrompt import ChatSessionPrompt
from src.deps import auth_dependency, db_dependency
from src.rate_limit import limiter
from src.agent_runtime.contact_agent_config import CONTACT_AGENT_CONFIG
from src.agent_runtime.stream import generator

router = APIRouter()


@router.post("/{session_id}/stream")
@limiter.limit("200/minute")
async def contact_chat_stream(
    session_id: str,
    request_body: ChatSessionPrompt,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    """Stream the code-defined, contact-scoped CRM agent for one session.

    The path `session_id` is authoritative (not request_body.sessionId).
    """
    return StreamingResponse(
        generator(
            agent_id=None,
            session_id=session_id,
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
            agent_config_override=CONTACT_AGENT_CONFIG,
        ),
        media_type="text/event-stream",
    )
