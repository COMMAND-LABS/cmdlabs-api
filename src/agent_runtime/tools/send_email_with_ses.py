"""Send Plain-Text Email Tool via AWS SES — HITL variant."""

from typing import Any

from langchain_core.tools import StructuredTool
from sqlalchemy.orm import Session

from src.agent_runtime.tools.hitl_email_base import create_hitl_plain_email_tool


async def create_send_email_with_ses_tool(
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
        tool_type="sendTxtEmailWithSes",
        tool_name="send_txt_email_with_ses",
        required_credential_fields=["aws_access_key_id", "aws_secret_access_key", "aws_region", "from_email"],
        provider_label="AWS SES",
        default_description=(
            "Send a plain-text email to a recipient. "
            "The email will be reviewed by a human before it is delivered."
        ),
        **kwargs,
    )
