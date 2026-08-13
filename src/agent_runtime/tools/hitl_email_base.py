"""Shared building blocks for HITL-gated email tools.

Every email tool (plain-text SES and templated HTML SES) follows the same
pattern: verify a credential at build time, then queue a PendingToolApproval at
invocation time.  This module captures that shared logic — most importantly
:func:`queue_tool_approval`, the single place that writes the approval row and
builds the HITL sentinel response.
"""

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from src.db.models import PendingToolApproval
from src.routers.credentials.encryption import decrypt_credential_data
from src.services.credential_access import load_credential_for_use
from src.agent_runtime.tools.exceptions import CredentialError
from src.agent_runtime.tools.sessions import (
    default_session_factory,
    resolve_session_factory,
)

logger = logging.getLogger(__name__)

HITL_SENTINEL_KEY = "__approval_required__"
APPROVAL_TTL_MINUTES = 30


async def queue_tool_approval(
    *,
    account_id: int,
    agent_id: int | None,
    chat_session_id: int | None,
    tool_type: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    message: str,
    session_factory: Callable[[], Session] = default_session_factory,
) -> str:
    """Insert a ``PendingToolApproval`` row and return the HITL sentinel JSON.

    Shared by every HITL email tool. On DB failure, returns an error JSON
    (``{"success": False, ...}``) rather than raising, so a queuing problem
    surfaces to the agent as a tool result instead of a crash.
    """
    expires_at = datetime.now(UTC) + timedelta(minutes=APPROVAL_TTL_MINUTES)

    approval_db: Session = session_factory()
    try:
        approval = PendingToolApproval(
            account_id=account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            tool_type=tool_type,
            status="pending",
            payload=payload,
            expires_at=expires_at,
        )
        approval_db.add(approval)
        approval_db.commit()
        approval_db.refresh(approval)
        approval_id = approval.id
        logger.info(f"[HITL EMAIL] Queued {tool_type} approval id={approval_id}")
    except Exception as exc:
        approval_db.rollback()
        logger.exception(f"[HITL EMAIL] Failed to queue {tool_type} approval")
        return json.dumps({"success": False, "error": f"Failed to queue email for approval: {exc}"})
    finally:
        approval_db.close()

    return json.dumps({
        HITL_SENTINEL_KEY: True,
        "approval_id": approval_id,
        "tool_type": tool_type,
        "preview": preview,
        "message": message,
    })


def verify_credential(
    credential_id: int,
    account_id: int,
    db: Session,
    required_fields: list[str],
    provider_label: str,
) -> str:
    """Validate a credential and return its ``from_email`` value.

    Raises ``CredentialError`` if anything is wrong.
    """
    # Usage access: owned or shared with the account (plaintext never leaves the server).
    credential = load_credential_for_use(db, account_id, credential_id)

    if not credential:
        raise CredentialError(f"Credential {credential_id} not found or not accessible.")

    try:
        data = decrypt_credential_data(credential.encrypted_data)
    except Exception as exc:
        raise CredentialError(f"Failed to decrypt credential {credential_id}: {exc}") from exc

    missing = [k for k in required_fields if not data.get(k)]
    if missing:
        raise CredentialError(
            f"Credential {credential_id} is missing required {provider_label} fields: {missing}. "
            f"Available keys: {list(data.keys())}"
        )

    return data["from_email"]


class _SendEmailInput(BaseModel):
    to_email: str = Field(description="Recipient email address (e.g. user@example.com)")
    subject: str = Field(description="Email subject line")
    body: str = Field(description="Plain-text email body")


async def create_hitl_plain_email_tool(
    *,
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    tool_type: str,
    tool_name: str,
    required_credential_fields: list[str],
    provider_label: str,
    default_description: str,
    **kwargs,
) -> StructuredTool:
    """Generic factory for HITL-gated plain-text email tools."""
    credential_id = tool_config.get("credentialId")
    description = tool_config.get("description") or default_description

    if not credential_id:
        raise CredentialError(
            f"Missing required field 'credentialId' in {tool_type} tool configuration"
        )

    credential_account_id = kwargs.get("agent_owner_account_id", account_id)
    from_email = verify_credential(
        credential_id, credential_account_id, db, required_credential_fields, provider_label,
    )

    agent_id: int | None = kwargs.get("agent_id")
    chat_session_id: int | None = kwargs.get("chat_session_id_pk")
    session_factory = resolve_session_factory(kwargs)

    async def send_email_impl(to_email: str, subject: str, body: str) -> str:
        """Queue an email for human approval before sending."""
        return await queue_tool_approval(
            account_id=credential_account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            session_factory=session_factory,
            tool_type=tool_type,
            payload={
                "credential_id": credential_id,
                "to_email": to_email,
                "subject": subject,
                "body": body,
            },
            preview={
                "from_email": from_email,
                "to_email": to_email,
                "subject": subject,
                "body": body,
            },
            message=(
                f"Email to {to_email} has been queued for human review. "
                "It will be sent only after the user approves it."
            ),
        )

    return StructuredTool(
        func=send_email_impl,
        coroutine=send_email_impl,
        name=tool_name,
        description=description,
        args_schema=_SendEmailInput,
    )
