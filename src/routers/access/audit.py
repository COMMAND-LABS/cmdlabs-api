"""
Access audit endpoints.

Turns the access graph into something you can read off one screen:
- effective users of a resource (grants resolved to individual accounts), and
- for an agent, the DERIVED exposure: the indexes its vector-search tools query
  and whose source documents its users can therefore open — the implicit chain
  that has no grant row of its own.
"""
from typing import List, Optional
from fastapi import APIRouter, HTTPException, status, Request
from pydantic import BaseModel
from sqlalchemy import tuple_

from datetime import datetime
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import AGENT, VECTOR_STORE, resource_predicate, scoped_resources
from src.db.models import Agent, VectorStore, Credential, AccessGrant, AccessGrantEvent
from src.services import access
from src.services.access_admin import grant_label
from src.rate_limit import limiter

router = APIRouter()

_VECTOR_TOOL_TYPES = {"vectorSearch", "vectorSearchWithReranking"}


class EffectiveAccount(BaseModel):
    account_id: int
    email: Optional[str] = None
    role: str
    via: str  # 'owner' | 'direct' | 'group:<name>'


class DerivedExposure(BaseModel):
    """A resource reachable THROUGH the audited resource (no grant of its own)."""
    resource_type: str
    resource_id: int
    label: str
    note: str


class ResourceAuditResponse(BaseModel):
    resource_type: str
    resource_id: int
    effective_accounts: List[EffectiveAccount]
    derived_exposure: List[DerivedExposure]


class ReverseAuditItem(BaseModel):
    resource_type: str
    resource_id: int
    label: str
    role: str
    via: str


class SharedGrant(BaseModel):
    # Space shares used to appear here too, carrying a NEGATIVE id: the two
    # lived in different tables with their own id sequences, so a positive id
    # alone would have been ambiguous to a reader deciding what to revoke.
    # Every id here is a positive access_grants id now.
    grant_id: int
    label: str          # the grantee's email
    role: str
    # 'person', and currently nothing else. Kept because the value it existed to
    # distinguish — 'space', a reach that deliberately left the org — is the
    # thing a reader of this page most needs told apart, and the field should be
    # here waiting rather than reintroduced along with it.
    reach: str = "person"


class SharedResource(BaseModel):
    resource_type: str
    resource_id: int
    label: str
    shared_with: List[SharedGrant]


def _require_owner(db, account_id: int, resource_type: str, resource_id: int):
    """Only a resource's owner may audit who can access it."""
    owner = access._resource_owner(db, resource_type, resource_id)
    if owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resource not found")
    if owner != account_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can audit access to this resource")


@router.get("/resources/{resource_type}/{resource_id}/audit", response_model=ResourceAuditResponse)
@limiter.limit("30/minute")
async def audit_resource(
    resource_type: str,
    resource_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Who can access this resource (resolved to accounts) + derived exposure.

    For an agent, derived_exposure lists each index its vector-search tools query
    — those same effective accounts can read that index's content and open its
    source documents (via /files/source-url), even with no grant on the index.
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)
    if resource_type not in (access.AGENT, access.VECTOR_STORE, access.CREDENTIAL):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown resource type")
    _require_owner(db, account_id, resource_type, resource_id)

    effective = [EffectiveAccount(**e) for e in access.effective_accounts(db, resource_type, resource_id)]

    derived: List[DerivedExposure] = []
    if resource_type == access.AGENT:
        agent = db.query(Agent).filter(Agent.id == resource_id).first()
        tools = ((agent.config or {}).get("data") or {}).get("tools") or [] if agent else []
        seen = set()
        for tool in tools:
            if isinstance(tool, dict) and tool.get("type") in _VECTOR_TOOL_TYPES:
                idx = tool.get("index")
                if not idx or idx in seen:
                    continue
                seen.add(idx)
                vs = (
                    db.query(VectorStore)
                    .filter(VectorStore.owner_account_id == agent.account_id, VectorStore.index_name == idx)
                    .first()
                )
                derived.append(DerivedExposure(
                    resource_type=access.VECTOR_STORE,
                    resource_id=vs.id if vs else -1,
                    label=idx,
                    note="Agent users can read this index's content and open its source documents.",
                ))

    return ResourceAuditResponse(
        resource_type=resource_type,
        resource_id=resource_id,
        effective_accounts=effective,
        derived_exposure=derived,
    )


@router.get("/report", response_model=List[ReverseAuditItem])
@limiter.limit("30/minute")
async def my_access_report(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Reverse audit: everything the caller can reach via grants, and the path."""
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    items = access.resources_for_account(db, account_id)
    out: List[ReverseAuditItem] = []
    for it in items:
        label = _resource_label(db, it["resource_type"], it["resource_id"])
        out.append(ReverseAuditItem(
            resource_type=it["resource_type"],
            resource_id=it["resource_id"],
            label=label,
            role=it["role"],
            via=it["via"],
        ))
    return out


@router.get("/shared-by-me", response_model=List[SharedResource])
@limiter.limit("30/minute")
async def shared_by_me(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Every resource the caller OWNS that is shared, with whom, and how far.

    ONE ARM today: grants, which never leave the org. There was a second for
    space shares, listed side by side and labelled with its reach, on the
    principle that a report showing only the org-confined arm is worse than no
    report — the arm it omits is the one that leaves the organization. That
    principle outlives spaces: anything that restores cross-org reach must
    appear here in the same pass, not on a page of its own.
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)

    # Map each owned resource (type, id) -> label.
    owned: dict = {}
    for aid, name in db.query(Agent.id, Agent.name).filter(resource_predicate(Agent, org)).all():
        owned[(access.AGENT, aid)] = name
    for vid, idx in db.query(VectorStore.id, VectorStore.index_name).filter(resource_predicate(VectorStore, org)).all():
        owned[(access.VECTOR_STORE, vid)] = idx
    for cid, cname, ctype in db.query(Credential.id, Credential.credential_name, Credential.credential_type).filter(Credential.account_id == account_id).all():
        owned[(access.CREDENTIAL, cid)] = cname or str(ctype)
    if not owned:
        return []

    # Pull all grants on those resources in one pass, grouped by resource.
    by_resource: dict = {}
    grants = (
        db.query(AccessGrant)
        .filter(AccessGrant.resource_type.in_([access.AGENT, access.VECTOR_STORE, access.CREDENTIAL]))
        .all()
    )
    for g in grants:
        key = (g.resource_type, g.resource_id)
        if key not in owned:
            continue
        by_resource.setdefault(key, []).append(
            SharedGrant(grant_id=g.id, label=grant_label(db, g),
                        role=g.role, reach="person")
        )

    return [
        SharedResource(
            resource_type=rtype,
            resource_id=rid,
            label=owned[(rtype, rid)],
            shared_with=shares,
        )
        for (rtype, rid), shares in by_resource.items()
    ]


class AccessEvent(BaseModel):
    id: int
    event_type: str          # 'create' | 'revoke' | 'role_change'
    resource_type: str
    resource_id: int
    resource_label: Optional[str] = None
    principal_type: str
    principal_label: Optional[str] = None
    role: Optional[str] = None
    actor_email: Optional[str] = None
    created_at: datetime


@router.get("/activity", response_model=List[AccessEvent])
@limiter.limit("30/minute")
async def access_activity(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
    limit: int = 200,
):
    """Append-only audit log of access changes on resources the caller OWNS.

    Shows who granted/revoked/changed access (and when) to your agents, knowledge
    bases, and credentials — including revokes, which the live grant tables can't.
    """
    account_id = account_id_from_claims(jwt)
    ensure_account(db, account_id)
    limit = max(1, min(limit, 500))

    # Resource keys the caller owns.
    owned = set()
    owned |= {(access.AGENT, r[0]) for r in db.query(Agent.id).filter(resource_predicate(Agent, org)).all()}
    owned |= {(access.VECTOR_STORE, r[0]) for r in db.query(VectorStore.id).filter(resource_predicate(VectorStore, org)).all()}
    owned |= {(access.CREDENTIAL, r[0]) for r in db.query(Credential.id).filter(Credential.account_id == account_id).all()}
    if not owned:
        return []

    # Pull recent events on the caller's own resources, then keep those on
    # resources they own.
    #
    # The (type, id) pairs are filtered in SQL rather than only in Python.
    # This used to take the newest 2000 rows platform-wide and filter after
    # the fact, which was fine while the table held nothing but resource
    # grants — but the log now also carries membership, tier, ceiling, org
    # and catalog events for every org, and those would steadily crowd the
    # caller's own grant history out of the window. It also stops the scan
    # being O(every org) to answer a question about one.
    rows = (
        db.query(AccessGrantEvent)
        .filter(
            tuple_(AccessGrantEvent.resource_type,
                   AccessGrantEvent.resource_id).in_(list(owned)),
        )
        .order_by(AccessGrantEvent.created_at.desc())
        .limit(limit)
        .all()
    )
    out = [
        AccessEvent(
            id=e.id,
            event_type=e.event_type,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            resource_label=e.resource_label,
            principal_type=e.principal_type,
            principal_label=e.principal_label,
            role=e.role,
            actor_email=e.actor_email,
            created_at=e.created_at,
        )
        for e in rows
    ]
    return out


def _resource_label(db, resource_type: str, resource_id: int) -> str:
    if resource_type == access.AGENT:
        row = db.query(Agent.name).filter(Agent.id == resource_id).first()
        return row[0] if row else f"agent #{resource_id}"
    if resource_type == access.VECTOR_STORE:
        row = db.query(VectorStore.index_name).filter(VectorStore.id == resource_id).first()
        return row[0] if row else f"vector_store #{resource_id}"
    if resource_type == access.CREDENTIAL:
        row = db.query(Credential.credential_name, Credential.credential_type).filter(Credential.id == resource_id).first()
        if row:
            return row[0] or str(row[1])
        return f"credential #{resource_id}"
    return f"{resource_type} #{resource_id}"
