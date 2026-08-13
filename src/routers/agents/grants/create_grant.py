"""
Share an agent with one named person (agent owner only).

Writes a unified AccessGrant (resource_type='agent', role='use'). Sharing with
a group of people was putting the agent in a space instead; spaces are gone.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.services.org_scope import AGENT, VECTOR_STORE, resource_predicate, scoped_resources
from src.db.models import Agent, AccessGrant
from src.services import access
from src.services.access_admin import resolve_grantee, upsert_grant, record_access_event
from .models import CreateGrantRequest, AgentAccessGrantResponse
from src.rate_limit import limiter

router = APIRouter()

@router.post("/{agent_id}/access-grants", response_model=AgentAccessGrantResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def create_grant(
    agent_id: int,
    body: CreateGrantRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request,
):
    """Let one other person in this org use this agent. Agent owner only."""
    account_id = account_id_from_claims(jwt)

    agent = db.query(Agent).filter(
        Agent.id == agent_id,
        resource_predicate(Agent, org),
    ).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    principal_type, principal_id, label = resolve_grantee(
        db,
        caller_account_id=account_id,
        grantee_email=body.granteeEmail,
    )

    existing = db.query(AccessGrant).filter(
        AccessGrant.principal_type == principal_type,
        AccessGrant.principal_id == principal_id,
        AccessGrant.resource_type == access.AGENT,
        AccessGrant.resource_id == agent_id,
    ).first()
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent is already shared with this person")

    grant = upsert_grant(
        db,
        org_id=org.org_id,
        principal_type=principal_type,
        principal_id=principal_id,
        resource_type=access.AGENT,
        resource_id=agent_id,
        role="use",
    )
    record_access_event(
        db,
        event_type="create",
        actor_account_id=account_id,
        resource_type=access.AGENT,
        resource_id=agent_id,
        principal_type=principal_type,
        principal_id=principal_id,
        role="use",
    )
    db.commit()
    db.refresh(grant)

    return AgentAccessGrantResponse(
        id=grant.id,
        agent_id=agent_id,
        grantee_account_id=principal_id,
        label=label,
        created_at=grant.created_at,
    )
