"""
Share a credential with one named person (credential owner only).

A credential was never shareable into a space: it is an API key with a bill
attached, and a space's audience crossed orgs by design. Keep that true of
whatever cross-org sharing replaces spaces.

Writes a unified AccessGrant (resource_type='credential', role='use').
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.db.models import Credential, AccessGrant
from src.services import access
from src.services.access_admin import resolve_grantee, upsert_grant, record_access_event
from .models import CreateCredentialGrantRequest, CredentialGrantResponse
from src.rate_limit import limiter

router = APIRouter()


@router.post("/{credential_id}/access-grants", response_model=CredentialGrantResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_credential_grant(
    credential_id: int,
    body: CreateCredentialGrantRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Share a credential with one other person. Owner only. Use-not-view."""
    account_id = account_id_from_claims(jwt)

    credential = db.query(Credential).filter(
        Credential.id == credential_id,
        Credential.account_id == account_id,
    ).first()
    if not credential:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found")

    principal_type, principal_id, label = resolve_grantee(
        db,
        caller_account_id=account_id,
        grantee_email=body.granteeEmail,
    )

    # Reject duplicate (a grant already exists for this person on this credential).
    existing = db.query(AccessGrant).filter(
        AccessGrant.principal_type == principal_type,
        AccessGrant.principal_id == principal_id,
        AccessGrant.resource_type == access.CREDENTIAL,
        AccessGrant.resource_id == credential_id,
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Credential is already shared with that person")

    grant = upsert_grant(
        db,
        org_id=org.org_id,
        principal_type=principal_type,
        principal_id=principal_id,
        resource_type=access.CREDENTIAL,
        resource_id=credential_id,
        role="use",
    )
    record_access_event(
        db,
        event_type="create",
        actor_account_id=account_id,
        resource_type=access.CREDENTIAL,
        resource_id=credential_id,
        principal_type=principal_type,
        principal_id=principal_id,
        role="use",
    )
    db.commit()
    db.refresh(grant)

    return CredentialGrantResponse(
        id=grant.id,
        credential_id=credential_id,
        grantee_account_id=principal_id,
        label=label,
        created_at=grant.created_at,
    )
