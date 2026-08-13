"""Send Templated HTML Email Tool via AWS SES — HITL variant.

Preferred: template-based (agent picks an EmailTemplate by ID + variable values).
Fallback: raw HTML when no suitable template exists.
"""

import json
import logging
import re
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from src.db.models import EmailTemplate
from src.agent_runtime.tools.exceptions import CredentialError
from src.agent_runtime.tools.hitl_email_base import queue_tool_approval, verify_credential

logger = logging.getLogger(__name__)


def _render(template: str, variables: dict[str, str]) -> str:
    """Replace {{ token }} placeholders — tolerates optional spaces around the name."""
    return re.sub(
        r'\{\{\s*(\w+)\s*\}\}',
        lambda m: variables.get(m.group(1), m.group(0)),
        template,
    )


def _build_template_catalogue(db: Session, account_id: int) -> str:
    try:
        templates = (
            db.query(EmailTemplate)
            .filter(EmailTemplate.account_id == account_id)
            .order_by(EmailTemplate.name)
            .all()
        )
        if not templates:
            return "  (no templates saved yet — create one in the Email Templates dashboard)"
        lines = []
        for t in templates:
            vars_str = ", ".join(v["name"] for v in (t.variables or []))
            lines.append(
                f"  • ID {t.id}: {t.name}"
                + (f"  |  variables: {vars_str}" if vars_str else "")
                + (f"\n    {t.description}" if t.description else "")
            )
        return "\n".join(lines)
    except Exception:
        return "  (unable to list templates)"


async def create_send_html_email_with_ses_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Session,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    """
    Create the HITL-gated HTML email tool.

    Required tool_config keys:
        - credentialId: int — ID of the stored AWS SES credential
        - description:  str (optional) — extra LLM guidance
    """
    credential_id = tool_config.get("credentialId")
    if not credential_id:
        raise CredentialError(
            "Missing required field 'credentialId' in sendHtmlEmailWithSes tool configuration"
        )

    credential_account_id = kwargs.get("agent_owner_account_id", account_id)
    from_email = verify_credential(
        credential_id, credential_account_id, db,
        ["aws_access_key_id", "aws_secret_access_key", "aws_region", "from_email"],
        "AWS SES",
    )

    agent_id: int | None = kwargs.get("agent_id")
    chat_session_id: int | None = kwargs.get("chat_session_id_pk")

    # Build a live catalogue of templates for the LLM description
    catalogue = _build_template_catalogue(db, credential_account_id)

    user_description = tool_config.get("description", "")
    description = (
        (f"{user_description}\n\n" if user_description else "")
        + "Send a professional HTML email via AWS SES.  The email requires human "
        "approval before it is delivered.\n\n"
        "━━ PREFERRED: use a saved template ━━\n"
        "Pass `template_id` (integer) and `variables` (object with token→value pairs).\n"
        "The template is rendered server-side — consistent layout, tracked opens.\n\n"
        f"Available templates:\n{catalogue}\n\n"
        "━━ FALLBACK: raw HTML ━━\n"
        "Only use `html_body` when no suitable template exists.  Omit `template_id`.\n"
        "The HTML must be a self-contained, inline-CSS, table-layout document ≤600 px wide."
    )

    from src.db.database import SessionLocal

    logger.info(
        f"[SEND HTML EMAIL TOOL] ready — "
        f"credential_id={credential_id}, account_id={credential_account_id}"
    )

    async def queued_send(
        to_email: str,
        template_id: int | None = None,
        variables: dict[str, str] | None = None,
        html_body: str | None = None,
        subject: str | None = None,
    ) -> str:
        """Resolve template / raw HTML, then create the PendingToolApproval."""
        template_name: str | None = None
        merged_variables: dict[str, str] = {}

        # ── Template mode ──────────────────────────────────────────────────────
        if template_id is not None:
            tool_db: Session = SessionLocal()
            try:
                tmpl = tool_db.query(EmailTemplate).filter(
                    EmailTemplate.id == template_id,
                    EmailTemplate.account_id == credential_account_id,
                ).first()
            finally:
                tool_db.close()

            if not tmpl:
                return json.dumps({
                    "success": False,
                    "error": (
                        f"Template ID {template_id} not found. "
                        "Use one of the IDs shown in this tool's description."
                    ),
                })

            for v in (tmpl.variables or []):
                merged_variables[v["name"]] = v.get("default", "")
            merged_variables.update({k: str(val) for k, val in (variables or {}).items()})

            html_body = _render(tmpl.html_template, merged_variables)
            # Subject always comes from the template — LLM must not override it
            subject = _render(tmpl.subject_template, merged_variables)
            template_name = tmpl.name

        # ── Raw HTML mode ──────────────────────────────────────────────────────
        else:
            if not html_body or not html_body.strip():
                return json.dumps({
                    "success": False,
                    "error": (
                        "Either 'template_id' or 'html_body' must be provided. "
                        "Prefer template_id — see the available templates in this tool's description."
                    ),
                })
            if not subject or not subject.strip():
                return json.dumps({
                    "success": False,
                    "error": "'subject' is required when not using a template.",
                })

        logger.info(
            f"[SEND HTML EMAIL TOOL] 📬 queuing for approval — "
            f"To: {to_email}, Subject: {subject}, template_id: {template_id}"
        )
        logger.debug(f"[SEND HTML EMAIL TOOL]   HTML bytes: {len(html_body or '')}")

        return await queue_tool_approval(
            account_id=credential_account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            tool_type="sendHtmlEmailWithSes",
            payload={
                "credential_id": credential_id,
                "to_email": to_email,
                "subject": subject,
                "html_body": html_body,
                # Template metadata stored for reference / audit
                "template_id": template_id,
                "template_name": template_name,
                "variables": merged_variables if merged_variables else None,
            },
            preview={
                "from_email": from_email,
                "to_email": to_email,
                "subject": subject,
                "html_body": html_body,
                "template_name": template_name,
                "variables": merged_variables if merged_variables else None,
            },
            message=(
                f"{'Template ' + repr(template_name) + ' email' if template_name else 'HTML email'} "
                f"to {to_email} has been queued for human review. "
                "It will be sent only after the user approves it."
            ),
        )

    class SendHtmlEmailInput(BaseModel):
        to_email: str = Field(
            description="Recipient email address (e.g. user@example.com)"
        )
        template_id: int | None = Field(
            default=None,
            description=(
                "ID of a saved email template — STRONGLY PREFERRED. "
                "The subject line and HTML body are derived entirely from the template; "
                "do NOT provide a separate subject when using a template. "
                "Look up the available templates listed in this tool's description."
            ),
        )
        variables: dict[str, str] | None = Field(
            default=None,
            description=(
                "Variable values to inject into the template. "
                "Keys must match the token names defined on the template. "
                'Example: {"first_name": "Alex", "body": "Your order shipped today."}'
            ),
        )
        html_body: str | None = Field(
            default=None,
            description=(
                "Complete, self-contained HTML email body. "
                "Only use when no suitable template exists. "
                "Must use inline CSS and a table-based layout ≤ 600 px wide."
            ),
        )
        subject: str | None = Field(
            default=None,
            description=(
                "Email subject line — required ONLY when using raw html_body (no template). "
                "When template_id is provided the subject is set by the template automatically; "
                "do not provide this field."
            ),
        )

        @model_validator(mode="after")
        def validate_inputs(self) -> "SendHtmlEmailInput":
            if self.template_id is None:
                if not (self.html_body and self.html_body.strip()):
                    raise ValueError(
                        "Provide either 'template_id' (preferred) or 'html_body' (fallback)."
                    )
                if not (self.subject and self.subject.strip()):
                    raise ValueError(
                        "'subject' is required when using raw html_body instead of a template."
                    )
            return self

    return StructuredTool(
        func=queued_send,
        coroutine=queued_send,
        name="send_html_email_with_ses",
        description=description,
        args_schema=SendHtmlEmailInput,
    )
