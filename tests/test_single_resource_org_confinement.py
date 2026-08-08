"""
Fetching ONE resource by id is org-confined, not just listing them.

accessible_resource_ids() — the list path — was confined to an org; can_access()
— the fetch-one path — was not. That is the worse half to leave open: a caller
who already knows an id never touches the list endpoint.

Two arms had to be closed, and only one of them is about grants:

  1. the GRANT lookup, so a row recorded in another org does not count;
  2. the OWNER short-circuit, which returned True on `owner == account_id`
     before looking at any org at all.

Arm 2 is the subtle one. Ownership is not tenancy. It bites as soon as one
account belongs to two orgs — platform super admins who joined a tenant to
support it, today; anyone at all once org switching ships — because such an
account would reach its resources in either org whatever org it was currently
acting in, and the whole point of the active org is that it decides what you
see.

Credentials are deliberately exempt: they are portable identity rather than
tenant data, so services.access._resource_org returns None for them and the
confinement passes through. A member who leaves an org keeps their own API key.
"""
from sqlalchemy.orm import Session

from src.db.models import Agent, Organization, OrganizationMember
from src.services import access
from src.services.agent_access import can_access_agent
from tests.org_isolation import client_for, make_tenant


def _join(db: Session, account_id: int, org: Organization,
          role: str = "manager"):
    """Put an existing account into a second org, as super admins would.

    No per-org setup first: roles are constants, so the membership row is the
    whole act of joining.
    """
    db.add(OrganizationMember(org_id=org.id, account_id=account_id,
                              role=role, granted_by="grant"))
    db.flush()


async def test_owning_an_agent_elsewhere_does_not_reach_it_from_here(
    db: Session, _override_db
):
    """The owner short-circuit must not outrank the org check."""
    home = make_tenant(db, slug="conf-home", account_id=9501, data_scope="shared")
    away = make_tenant(db, slug="conf-away", account_id=9502, data_scope="shared")

    # One account, two orgs — and an agent it OWNS, living in the other one.
    _join(db, home.account_id, away.org, role="manager")
    agent = Agent(org_id=away.org_id, account_id=home.account_id,
                  name="Elsewhere", config={})
    db.add(agent)
    db.flush()

    assert can_access_agent(db, home.account_id, agent.id, org_id=away.org_id)
    assert not can_access_agent(db, home.account_id, agent.id, org_id=home.org_id), (
        "owning a resource in another org must not make it reachable from this one")

    async with client_for(home) as c:
        resp = await c.get(f"/api/agents/{agent.id}")
    assert resp.status_code == 404


async def test_a_grant_recorded_in_another_org_does_not_count(
    db: Session, _override_db
):
    """The read-side half of the same boundary.

    assert_same_org stops such a row being written now; this proves one written
    before that check landed is inert rather than live.
    """
    home = make_tenant(db, slug="conf-g-home", account_id=9503, data_scope="shared")
    away = make_tenant(db, slug="conf-g-away", account_id=9504, data_scope="shared")
    _join(db, home.account_id, away.org, role="manager")

    agent = Agent(org_id=away.org_id, account_id=away.account_id,
                  name="Theirs", config={})
    db.add(agent)
    db.flush()

    # Written directly, bypassing upsert_grant — this is the historical row.
    from src.db.models import AccessGrant
    db.add(AccessGrant(org_id=home.org_id, principal_type=access.ACCOUNT,
                       principal_id=home.account_id, resource_type=access.AGENT,
                       resource_id=agent.id, role="use"))
    db.flush()

    assert not can_access_agent(db, home.account_id, agent.id, org_id=home.org_id), (
        "a grant naming a resource in another org must not resolve")

    async with client_for(home) as c:
        resp = await c.get(f"/api/agents/{agent.id}")
    assert resp.status_code == 404


async def test_credentials_stay_portable(db: Session, _override_db):
    """Confinement must not follow a credential, which is identity rather than
    tenant data — the owner keeps it when they leave."""
    from src.db.models import Credential, ServiceName

    tenant = make_tenant(db, slug="conf-cred", account_id=9505, data_scope="shared")
    other = make_tenant(db, slug="conf-cred-2", account_id=9506, data_scope="shared")

    cred = Credential(account_id=tenant.account_id,
                      credential_type=ServiceName.OPENAI_API_KEY,
                      credential_name="mine", encrypted_data="x")
    db.add(cred)
    db.flush()

    # Confined to a completely unrelated org, the owner still reaches it.
    assert access.can_access(db, tenant.account_id, access.CREDENTIAL, cred.id,
                             required="use", org_id=other.org_id)
