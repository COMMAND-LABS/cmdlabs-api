"""
Agent access control (thin compatibility layer over the unified resolver).

Access is now stored as AccessGrant rows and resolved by services/access.py. This
module keeps the original agent-centric helpers so existing call sites are
unchanged; each delegates to access.py with resource_type='agent', role='use'.

CANONICAL FILE. This module is mirrored byte-for-byte into kalygo3-agent-api
(src/services/agent_access.py). The ai-api copy is canonical; keep them in sync via
the repo-root scripts (./sync-schemas.sh).

Access rule: an account can access an agent if it OWNS it, or holds an
AccessGrant on it inside the org it is acting in, or belongs to a SPACE the
agent has been shared into.

The third arm is the only one that crosses an org boundary, and it is kept
textually separate from the other two for that reason — org_scope.shares_resource
is the one expression to review when asking "how can this leave its tenant?".
"""
from sqlalchemy.orm import Session

from src.db.models import Agent
from src.services import access
from src.services.org_scope import AGENT as _AGENT, shares_resource


def can_access_agent(db: Session, account_id: int, agent_id: int,
                     org_id: int | None = None) -> bool:
    """Return True if the account can view/use the agent.

    Pass org_id whenever a request context exists. Ownership is not tenancy:
    without it, an account that belongs to two orgs reaches its agent in either
    one whatever org it is currently acting in.
    """
    if access.can_access(db, account_id, access.AGENT, agent_id, required="use",
                         org_id=org_id):
        return True
    # Shared into a space this account is in. Deliberately NOT org-confined —
    # a space's members come from many orgs, which is what a space is for — and
    # deliberately last, so the org-confined answer is what is read first.
    return shares_resource(db, account_id, _AGENT, agent_id)


def get_accessible_agent_ids(db: Session, account_id: int, org_id: int | None = None) -> set:
    """Agent IDs the account can access via grants (excludes owned).

    Pass org_id whenever a request context exists: a grant carries no
    inherent tenancy, so an unconfined lookup can return an agent belonging
    to another organization.

    Grants only. Space shares are added by the LIST queries themselves, through
    org_scope.scoped_resources, which is also where the org predicate they have
    to sit beside lives. Adding them here as well would double-count them into
    one caller and leave another with the arm applied twice at different widths.
    """
    return access.accessible_resource_ids(db, account_id, access.AGENT, required="use",
                                          org_id=org_id)


def load_agent_with_access_check(
    db: Session,
    account_id: int,
    agent_id: int,
    org_id: int | None = None,
) -> "Agent | None":
    """Load and return the agent if *account_id* has access, else None."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if agent is None:
        return None
    # No owner short-circuit ahead of the org check: owning an agent in another
    # org does not make it reachable from this one. can_access() applies both.
    return agent if can_access_agent(db, account_id, agent_id, org_id=org_id) else None
