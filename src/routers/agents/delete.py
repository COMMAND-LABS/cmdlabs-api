"""
Delete agent endpoint.
"""
from fastapi import APIRouter, status, Request
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import get_resource_or_404
from src.db.models import Agent
from src.services import access
from src.services.access_admin import revoke_resource_grants_logged
from src.rate_limit import limiter

router = APIRouter()

@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def delete_agent(
    agent_id: int,
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """
    Delete an agent by ID.
    Only allows deleting agents belonging to the authenticated user.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)
        
    agent = get_resource_or_404(db, Agent, agent_id, org)

    # Remove sharing grants on this agent (polymorphic grants have no FK
    # cascade), logging a revoke event for each before the agent is gone.
    revoke_resource_grants_logged(
        db, resource_type=access.AGENT, resource_id=agent_id, actor_account_id=account_id
    )

    # Delete the agent
    db.delete(agent)
    db.commit()

    return None
