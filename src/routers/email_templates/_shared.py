"""The template lookup every email endpoint repeated.

Templates are looked up by (id, account_id): the account_id filter IS the
ownership check. Used by the CRUD routes here, the email-campaign routes and
POST /api/emails/send.
"""
from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.db.models import EmailTemplate


def owned_template_or_404(
    db: Session,
    template_id: int,
    account_id: int,
    *,
    detail: str = "Email template not found",
) -> EmailTemplate:
    """The caller's template by id, or 404 with ``detail``."""
    template = db.query(EmailTemplate).filter(
        EmailTemplate.id == template_id,
        EmailTemplate.account_id == account_id,
    ).first()
    if not template:
        raise HTTPException(status_code=404, detail=detail)
    return template
