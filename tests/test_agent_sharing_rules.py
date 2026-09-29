"""The rules for sharing an agent (READMEs/orgs_plans_and_sharing.md).

- Only the agent's owner can share it.
- Sharing requires the org to be on the premium plan.
- Everyone, on every plan and role, can manage their own API keys — a shared
  agent that does not run on its owner's model key asks for theirs.
"""
import pytest
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config.roles_registry import ROLE_COMMUNITY_MEMBER
from src.db.models import AccessGrant, Agent, Organization
from tests.org_isolation import client_for, make_tenant

ENTITLEMENTS = "/api/organizations/me/entitlements"


def _set_plan(db: Session, tenant, plan):
    org = db.query(Organization).filter(Organization.id == tenant.org_id).one()
    org.pinned_plan = plan
    db.flush()


@pytest.fixture()
def owner(db: Session):
    t = make_tenant(db, slug="share-rules", account_id=9101)
    _set_plan(db, t, plans.PLAN_PREMIUM)
    return t


@pytest.fixture()
def teammate(db: Session, owner):
    """A manager in the same org, who can SEE org-visible agents."""
    return make_tenant(db, slug="share-rules", account_id=9102, is_owner=False)


@pytest.fixture()
def invitee(db: Session, owner):
    """The person being shared with: a community member of the same org."""
    return make_tenant(db, slug="share-rules", account_id=9103,
                       role=ROLE_COMMUNITY_MEMBER, is_owner=False)


def _agent(db: Session, tenant, visibility="private"):
    agent = Agent(org_id=tenant.org_id, account_id=tenant.account_id,
                  name="Analyst", visibility=visibility, config={"data": {}})
    db.add(agent)
    db.flush()
    return agent


def _grants(db: Session, agent):
    return db.query(AccessGrant).filter(AccessGrant.resource_id == agent.id).count()


async def test_the_owner_can_share_on_premium(db: Session, _override_db, owner, invitee):
    agent = _agent(db, owner)
    async with client_for(owner) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": invitee.account.email})
    assert resp.status_code == 201, resp.text
    assert _grants(db, agent) == 1


async def test_a_teammate_who_can_see_the_agent_cannot_share_it(
    db: Session, _override_db, owner, teammate, invitee
):
    agent = _agent(db, owner, visibility="org")
    async with client_for(teammate) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": invitee.account.email})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Only the agent's owner can share it"
    assert _grants(db, agent) == 0


async def test_sharing_is_refused_on_the_free_plan(db: Session, _override_db, owner, invitee):
    _set_plan(db, owner, plans.PLAN_FREE)
    agent = _agent(db, owner)
    async with client_for(owner) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": invitee.account.email})
    # Today the Agents module itself is premium-only, so the module gate
    # refuses first (404). The explicit plan check in create_grant (403) keeps
    # sharing premium even if agents ever join the free plan. Either way: no
    # grant.
    assert resp.status_code in (403, 404), resp.text
    assert _grants(db, agent) == 0


async def test_a_community_member_can_manage_their_own_keys(
    db: Session, _override_db, owner, invitee
):
    async with client_for(invitee) as c:
        modules = (await c.get(ENTITLEMENTS)).json()["modules"]
        listed = await c.get("/api/credentials/")
    assert "credentials" in modules
    assert listed.status_code == 200, listed.text


async def test_a_free_account_can_manage_their_own_keys(db: Session, _override_db):
    solo = make_tenant(db, slug="share-rules-free", account_id=9104)
    _set_plan(db, solo, plans.PLAN_FREE)
    async with client_for(solo) as c:
        modules = (await c.get(ENTITLEMENTS)).json()["modules"]
        listed = await c.get("/api/credentials/")
    assert "credentials" in modules
    assert listed.status_code == 200, listed.text


async def test_db_write_connects_with_the_agent_owners_credential(monkeypatch):
    """A shared agent's database write uses the OWNER's credential, like every
    other tool, while injectAccountId still records the person chatting."""
    from src.agent_runtime.tools import db_write

    seen = {}

    def fake_connection_string(credential_id, account_id, db):
        seen["account_id"] = account_id
        return "sqlite://"

    monkeypatch.setattr(db_write, "get_connection_string", fake_connection_string)
    await db_write.create_db_write_tool(
        tool_config={"type": "dbTableWrite", "credentialId": 6, "table": "leads",
                     "columns": ["name"], "injectAccountId": True},
        account_id=9103,            # the person chatting
        db=None,
        agent_owner_account_id=9101,
    )
    assert seen["account_id"] == 9101


async def test_sharing_stays_premium_even_if_agents_reach_the_free_plan(
    db: Session, _override_db, owner, invitee, monkeypatch
):
    """The explicit plan check in create_grant, not just the module gate."""
    monkeypatch.setitem(plans.PLAN_MODULES, plans.PLAN_FREE,
                        plans.PLAN_MODULES[plans.PLAN_FREE] + ("agents",))
    _set_plan(db, owner, plans.PLAN_FREE)
    agent = _agent(db, owner)
    async with client_for(owner) as c:
        resp = await c.post(f"/api/agents/{agent.id}/access-grants",
                            json={"granteeEmail": invitee.account.email})
    assert resp.status_code == 403, resp.text
    assert resp.json()["detail"] == "Sharing agents requires the premium plan"
    assert _grants(db, agent) == 0


# ── revoking a share ────────────────────────────────────────────────────────

def _grant(db: Session, agent, grantee):
    g = AccessGrant(org_id=agent.org_id, resource_type="agent", resource_id=agent.id,
                    principal_type="account", principal_id=grantee.account_id,
                    role="use")
    db.add(g)
    db.flush()
    return g


async def test_the_owner_can_revoke_a_share(db: Session, _override_db, owner, invitee):
    agent = _agent(db, owner)
    grant = _grant(db, agent, invitee)
    async with client_for(owner) as c:
        resp = await c.delete(f"/api/agents/{agent.id}/access-grants/{grant.id}")
    assert resp.status_code == 204, resp.text
    assert _grants(db, agent) == 0


async def test_revoke_answers_not_found_for_anyone_but_the_owner(
    db: Session, _override_db, owner, teammate, invitee
):
    """Same answer for "not yours" and "does not exist", in this org or another."""
    agent = _agent(db, owner, visibility="org")
    grant = _grant(db, agent, invitee)
    outsider = make_tenant(db, slug="share-rules-elsewhere", account_id=9104)
    for caller in (teammate, invitee, outsider):
        async with client_for(caller) as c:
            resp = await c.delete(f"/api/agents/{agent.id}/access-grants/{grant.id}")
            missing = await c.delete(f"/api/agents/999999/access-grants/{grant.id}")
        assert resp.status_code == missing.status_code == 404, (caller.account_id, resp.text)
    assert _grants(db, agent) == 1
