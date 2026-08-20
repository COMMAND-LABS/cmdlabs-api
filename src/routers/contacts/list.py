"""
List contacts endpoint.
"""
from fastapi import APIRouter, Request, Query
from sqlalchemy import func as sqlfunc
from src.deps import org_dependency, db_dependency, auth_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import tenant_predicate
from src.db.models import Contact

from .models import ContactListResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=ContactListResponse)
@limiter.limit("60/minute")
async def list_contacts(
    db: db_dependency,
    auth: auth_dependency,
    org: org_dependency,
    request: Request,
    status_filter: str | None = Query(default=None, alias="status"),
    search: str | None = Query(default=None),
    limit: int = Query(50, ge=1, le=500, description="Number of contacts to return"),
    offset: int = Query(0, ge=0, description="Number of contacts to skip"),
    sort_by: str = Query(
        "updated",
        pattern="^(name|added|updated)$",
        description="Sort key: display name, created_at, or updated_at",
    ),
    sort_dir: str = Query(
        "desc", pattern="^(asc|desc)$", description="Sort direction"
    ),
):
    """
    List contacts for the authenticated user (server-side paginated).

    Supports optional filtering by ?status= and full-text ?search= over
    first/middle/last name and all emails (default + alternates), plus
    ?sort_by=name|added|updated with ?sort_dir=asc|desc (defaults preserve
    the original updated-desc order). Returns a paginated envelope
    ({contacts, total, limit, offset, has_more}).
    """
    account_id = account_id_from_claims(auth)
    account = ensure_account(db, account_id)

    query = db.query(Contact).filter(tenant_predicate(Contact, org))

    if status_filter:
        query = query.filter(Contact.status == status_filter)

    if search:
        term = f"%{search.lower()}%"
        query = query.filter(
            sqlfunc.lower(Contact.first_name).like(term)
            | sqlfunc.lower(Contact.middle_name).like(term)
            | sqlfunc.lower(Contact.last_name).like(term)
            | sqlfunc.lower(Contact.email).like(term)
            | sqlfunc.lower(Contact.alt_email_1).like(term)
            | sqlfunc.lower(Contact.alt_email_2).like(term)
        )

    # Name sorts the way the table displays it: "first last", case-blind.
    # last_name is nullable, so it coalesces rather than letting NULL sort
    # to the extremes.
    if sort_by == "name":
        sort_keys = [
            sqlfunc.lower(Contact.first_name),
            sqlfunc.lower(sqlfunc.coalesce(Contact.last_name, "")),
        ]
    elif sort_by == "added":
        sort_keys = [Contact.created_at]
    else:
        sort_keys = [Contact.updated_at]
    ordering = [
        key.asc() if sort_dir == "asc" else key.desc() for key in sort_keys
    ]
    # Deterministic tiebreak: without it, rows with equal keys can shuffle
    # between pages and paginated clients see duplicates/gaps.
    ordering.append(Contact.id.asc())

    # Total before pagination, then the requested slice.
    total = query.count()
    contacts = (
        query.order_by(*ordering)
        .offset(offset)
        .limit(limit)
        .all()
    )

    return ContactListResponse.of(contacts, total=total, limit=limit, offset=offset)
