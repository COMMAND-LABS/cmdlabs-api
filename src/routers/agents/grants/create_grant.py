"""
Share an agent with one named person.

Only the agent's OWNER may share it, and only in an org on the premium plan:
sharing is a premium feature, and on the free plan the person shared with
could not open agent chat anyway (see config/plans_registry). The person must
already be a member of this org (resolve_grantee / assert_same_org).

Writes a unified AccessGrant (resource_type='agent', role='use').
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims
from src.config import plans_registry as plans
from src.services.org_scope import get_resource_or_404
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

    agent = get_resource_or_404(db, Agent, agent_id, org)
    # Same check and wording as revoke_grant: seeing an org-visible agent is
    # not owning it.
    if agent.account_id != account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the agent's owner can share it",
        )
    if org.plan != plans.PLAN_PREMIUM:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sharing agents requires the premium plan",
        )

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
