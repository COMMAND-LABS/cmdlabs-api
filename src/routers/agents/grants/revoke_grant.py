"""
Revoke an agent access grant by grant id. Agent owner only.

It used to also admit a manager of the granted GROUP. Groups became spaces,
and a space share was revoked from the space by its owner (spaces have since
been removed altogether) — so the second authority moved
with the thing it was an authority over rather than being dropped.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims
from src.db.models import Agent, AccessGrant
from src.services import access
from src.services.access_admin import record_access_event
from src.rate_limit import limiter

router = APIRouter()

@router.delete("/{agent_id}/access-grants/{grant_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def revoke_grant(
    agent_id: int,
    grant_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """Revoke a grant on this agent. Agent owner only."""
    account_id = account_id_from_claims(jwt)

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    grant = db.query(AccessGrant).filter(
        AccessGrant.id == grant_id,
        AccessGrant.resource_type == access.AGENT,
        AccessGrant.resource_id == agent_id,
    ).first()
    if not grant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grant not found")

    if agent.account_id != account_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not have permission to revoke this grant")

    record_access_event(
        db,
        event_type="revoke",
        actor_account_id=account_id,
        resource_type=access.AGENT,
        resource_id=agent_id,
        principal_type=grant.principal_type,
        principal_id=grant.principal_id,
        role=grant.role,
    )
    db.delete(grant)
    db.commit()
    return None
