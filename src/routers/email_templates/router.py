"""Email templates CRUD router."""
from typing import List, Optional
from fastapi import APIRouter, status, Request, Query
from src.deps import db_dependency, auth_dependency, account_id_from_claims
from src.db.models import EmailTemplate
from ._shared import owned_template_or_404

from .models import (
    CreateEmailTemplateRequest,
    UpdateEmailTemplateRequest,
    EmailTemplateResponse,
)
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[EmailTemplateResponse])
@limiter.limit("60/minute")
async def list_email_templates(
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
    search: Optional[str] = Query(default=None, description="Filter by name (case-insensitive substring)"),
):
    account_id = account_id_from_claims(auth)
    q = db.query(EmailTemplate).filter(EmailTemplate.account_id == account_id)
    if search:
        q = q.filter(EmailTemplate.name.ilike(f"%{search}%"))
    return q.order_by(EmailTemplate.name).all()

@router.get("/{template_id}", response_model=EmailTemplateResponse)
@limiter.limit("60/minute")
async def get_email_template(
    template_id: int,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    tmpl = owned_template_or_404(db, template_id, account_id)
    return tmpl

@router.post("/", status_code=status.HTTP_201_CREATED, response_model=EmailTemplateResponse)
@limiter.limit("60/minute")
async def create_email_template(
    body: CreateEmailTemplateRequest,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    tmpl = EmailTemplate(
        account_id=account_id,
        name=body.name,
        description=body.description,
        subject_template=body.subject_template,
        html_template=body.html_template,
        variables=[v.model_dump() for v in body.variables] if body.variables else None,
    )
    db.add(tmpl)
    db.commit()
    db.refresh(tmpl)
    return tmpl

@router.patch("/{template_id}", response_model=EmailTemplateResponse)
@limiter.limit("60/minute")
async def update_email_template(
    template_id: int,
    body: UpdateEmailTemplateRequest,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    tmpl = owned_template_or_404(db, template_id, account_id)
    if body.name is not None:
        tmpl.name = body.name
    if body.description is not None:
        tmpl.description = body.description
    if body.subject_template is not None:
        tmpl.subject_template = body.subject_template
    if body.html_template is not None:
        tmpl.html_template = body.html_template
    if body.variables is not None:
        tmpl.variables = [v.model_dump() for v in body.variables]
    db.commit()
    db.refresh(tmpl)
    return tmpl

@router.delete("/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("60/minute")
async def delete_email_template(
    template_id: int,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    account_id = account_id_from_claims(auth)
    tmpl = owned_template_or_404(db, template_id, account_id)
    db.delete(tmpl)
    db.commit()
