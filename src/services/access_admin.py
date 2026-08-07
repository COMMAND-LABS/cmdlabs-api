"""
Grant administration helpers (ai-api only — agent-api never mutates grants).

Thin CRUD over AccessGrant used by the per-resource sharing endpoints, plus
grantee resolution (email → account). Keeping this in one place means every
sharing endpoint creates grants identically and the audit view reads a single
table.

A grant names ONE PERSON. Sharing with a set of people is sharing into a space
(routers/spaces, space_resources), which is a different table on purpose — see
the AccessGrant docstring. So there is no principal_type to resolve here any
more; there is only "which account is this email".
"""
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.db.models import (
    AccessGrant,
    AccessGrantEvent,
    Account,
    Agent,
    Credential,
    VectorStore,
)
from src.services import access


def resolve_grantee(
    db: Session,
    *,
    caller_account_id: int,
    grantee_email: str | None,
):
    """Resolve an email to (principal_type, account_id, label).

    Still returns a principal_type, and it is always access.ACCOUNT. Kept in
    the tuple rather than dropped so every call site keeps naming what it is
    writing into the polymorphic column — a bare id is how the wrong constant
    ends up there the day a second principal kind is added back.
    """
    if not grantee_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="granteeEmail is required",
        )

    target = db.query(Account).filter(Account.email == grantee_email).first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found for the given email")
    if target.id == caller_account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You already own this resource")
    return access.ACCOUNT, target.id, target.email


def upsert_grant(
    db: Session,
    *,
    principal_type: str,
    principal_id: int,
    resource_type: str,
    resource_id: int,
    role: str,
    org_id: int,
) -> AccessGrant:
    """Create the grant, or update its role if one already exists. Caller commits.

    The ONLY place AccessGrant rows are written, which is why the same-org
    check lives here: one chokepoint to guard rather than one per endpoint.
    """
    try:
        access.assert_same_org(db, org_id, principal_type, principal_id,
                               resource_type, resource_id)
    except access.CrossOrgGrantError as exc:
        # 404 rather than 403: confirming that a resource exists in another org
        # is itself a small leak.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Resource not found") from exc

    grant = (
        db.query(AccessGrant)
        .filter(
            AccessGrant.principal_type == principal_type,
            AccessGrant.principal_id == principal_id,
            AccessGrant.resource_type == resource_type,
            AccessGrant.resource_id == resource_id,
        )
        .first()
    )
    if grant:
        grant.role = role
    else:
        grant = AccessGrant(
            org_id=org_id,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=resource_type,
            resource_id=resource_id,
            role=role,
        )
        db.add(grant)
    return grant


def list_resource_grants(db: Session, resource_type: str, resource_id: int):
    """Raw grants on a resource (owner-facing management list)."""
    return (
        db.query(AccessGrant)
        .filter(AccessGrant.resource_type == resource_type, AccessGrant.resource_id == resource_id)
        .order_by(AccessGrant.created_at.desc())
        .all()
    )


def grant_label(db: Session, grant: AccessGrant) -> str:
    """Display label for a grant: the grantee's email."""
    return principal_label(db, grant.principal_type, grant.principal_id)


def principal_label(db: Session, principal_type: str, principal_id: int) -> str:
    """A name for the audit log, resolved at WRITE time and then snapshotted.

    Deliberately falls back to an id rather than raising: an event about a
    principal that has since been deleted still has to render, and "account
    #12" is a truthful thing to say about somebody who is gone.
    """
    row = db.query(Account.email).filter(Account.id == principal_id).first()
    return row[0] if row else f"account #{principal_id}"


def resource_label(db: Session, resource_type: str, resource_id: int) -> str:
    if resource_type == access.AGENT:
        row = db.query(Agent.name).filter(Agent.id == resource_id).first()
        return row[0] if row else f"agent #{resource_id}"
    if resource_type == access.VECTOR_STORE:
        row = db.query(VectorStore.index_name).filter(VectorStore.id == resource_id).first()
        return row[0] if row else f"knowledge base #{resource_id}"
    if resource_type == access.CREDENTIAL:
        row = db.query(Credential.credential_name, Credential.credential_type).filter(Credential.id == resource_id).first()
        if row:
            return row[0] or str(row[1])
        return f"credential #{resource_id}"
    return f"{resource_type} #{resource_id}"


def record_access_event(
    db: Session,
    *,
    event_type: str,
    actor_account_id: int,
    resource_type: str,
    resource_id: int,
    principal_type: str,
    principal_id: int,
    role: str | None,
) -> AccessGrantEvent:
    """
    Append an immutable audit event for a grant create/revoke/role_change,
    snapshotting actor email + principal/resource labels. Caller commits.

    org_id is derived from the RESOURCE rather than taken as a parameter. That
    keeps every call site unchanged and, more importantly, works on the revoke
    path too, where the grant row carrying the org is about to be deleted. A
    resource's org is the org the access change happened in, by definition.

    Credentials resolve to None on purpose: they are portable identity rather
    than tenant data, so a credential grant belongs to no single org.
    """
    actor_row = db.query(Account.email).filter(Account.id == actor_account_id).first()
    event = AccessGrantEvent(
        # Without this, grant events were the only rows in the log with a NULL
        # org while every services/audit.py row carried one — so the first
        # org-scoped view of the audit log would have silently omitted exactly
        # the events the table was built for.
        org_id=access._resource_org(db, resource_type, resource_id),
        event_type=event_type,
        resource_type=resource_type,
        resource_id=resource_id,
        resource_label=resource_label(db, resource_type, resource_id),
        principal_type=principal_type,
        principal_id=principal_id,
        principal_label=principal_label(db, principal_type, principal_id),
        role=role,
        actor_account_id=actor_account_id,
        actor_email=actor_row[0] if actor_row else None,
    )
    db.add(event)
    return event


def revoke_resource_grants_logged(
    db: Session, *, resource_type: str, resource_id: int, actor_account_id: int
) -> int:
    """Revoke every grant on a resource, logging a 'revoke' event for each.

    Use when a resource is deleted (the cascade cleanup) so those access changes
    still appear in the audit log. Call BEFORE deleting the resource row so its
    label snapshot resolves. Caller commits.
    """
    grants = (
        db.query(AccessGrant)
        .filter(AccessGrant.resource_type == resource_type, AccessGrant.resource_id == resource_id)
        .all()
    )
    for g in grants:
        record_access_event(
            db,
            event_type="revoke",
            actor_account_id=actor_account_id,
            resource_type=g.resource_type,
            resource_id=g.resource_id,
            principal_type=g.principal_type,
            principal_id=g.principal_id,
            role=g.role,
        )
    return access.revoke_grants_for_resource(db, resource_type, resource_id)


def revoke_principal_grants_logged(
    db: Session, *, principal_type: str, principal_id: int, actor_account_id: int
) -> int:
    """Revoke every grant held by a principal, logging a 'revoke' event for each.

    Use when an account is deleted. Call BEFORE deleting it so its label
    snapshot resolves. Caller commits.
    """
    grants = (
        db.query(AccessGrant)
        .filter(AccessGrant.principal_type == principal_type, AccessGrant.principal_id == principal_id)
        .all()
    )
    for g in grants:
        record_access_event(
            db,
            event_type="revoke",
            actor_account_id=actor_account_id,
            resource_type=g.resource_type,
            resource_id=g.resource_id,
            principal_type=g.principal_type,
            principal_id=g.principal_id,
            role=g.role,
        )
    return access.revoke_grants_for_principal(db, principal_type, principal_id)
