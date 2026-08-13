"""
List agents endpoint.

Returns agents the authenticated user owns as well as agents shared
with them via access groups.
"""
from fastapi import APIRouter, Request
from typing import List
from src.deps import org_dependency, db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.services.org_scope import AGENT, scoped_resources
from src.db.models import Agent
from src.services.agent_access import get_accessible_agent_ids
from .models import AgentResponse
from src.rate_limit import limiter

router = APIRouter()

@router.get("/", response_model=List[AgentResponse])
@limiter.limit("30/minute")
async def list_agents(
    db: db_dependency,
    jwt: jwt_dependency,
    org: org_dependency,
    request: Request
):
    """
    List all agents the authenticated user can access.

    This includes agents the user owns as well as agents shared with
    them via access groups.  Each response item includes ``is_owner``
    so the UI can distinguish owned vs. shared agents.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)
        
    # IDs the user can access via group grants (excludes owned)
    granted_ids = get_accessible_agent_ids(db, account_id, org_id=org.org_id)

    # Own org (honouring visibility) OR explicitly granted OR published to
    # this org through the catalog. The branch on `granted_ids` is gone:
    # scoped_resources composes the arms, so there is one query shape
    # rather than two that could drift apart.
    agents = (
        scoped_resources(db, Agent, org, AGENT, granted_ids)
        .order_by(Agent.id.desc())
        .all()
    )
        
    return [
        AgentResponse(
            id=agent.id,
            name=agent.name,
            config=agent.config,
            is_owner=(agent.account_id == account_id),
        )
        for agent in agents
    ]
