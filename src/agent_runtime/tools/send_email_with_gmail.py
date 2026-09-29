"""Send Plain-Text Email Tools via Gmail — HITL variants.

Two ways to send from Gmail, one builder each, both on the shared HITL
factory like the SES tool: the agent only QUEUES the email, and a person
approves it before routers/tool_approvals/approve.py sends it. The required
credential fields here are the ones approve.py's provider map reads, so a
credential that passes this check is one the approval step can send with.

These types were accepted by the agent config schema, offered by the UI and
sendable by approve.py, but never registered here, so the factory skipped
them as unknown and agents silently ran without them.
"""

from typing import Any

from langchain_core.tools import StructuredTool
from sqlalchemy.orm import Session

from src.agent_runtime.tools.hitl_email_base import create_hitl_plain_email_tool

_DEFAULT_DESCRIPTION = (
    "Send a plain-text email to a recipient from Gmail. "
    "The email will be reviewed by a human before it is delivered."
)


async def create_send_email_with_google_oauth_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    return await create_hitl_plain_email_tool(
        tool_config=tool_config,
        account_id=account_id,
        db=db,
        tool_type="sendTxtEmailWithGoogleOAuth",
        tool_name="send_txt_email_with_google_oauth",
        required_credential_fields=["client_id", "client_secret", "refresh_token", "from_email"],
        provider_label="Google OAuth",
        default_description=_DEFAULT_DESCRIPTION,
        **kwargs,
    )


async def create_send_email_with_google_smtp_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    return await create_hitl_plain_email_tool(
        tool_config=tool_config,
        account_id=account_id,
        db=db,
        tool_type="sendTxtEmailWithGoogleSmtp",
        tool_name="send_txt_email_with_google_smtp",
        required_credential_fields=["from_email", "app_password"],
        provider_label="Gmail SMTP",
        default_description=_DEFAULT_DESCRIPTION,
        **kwargs,
    )
