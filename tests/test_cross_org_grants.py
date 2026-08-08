"""
Grants may never cross an organization.

An AccessGrant row is polymorphic — (principal_type, principal_id,
resource_type, resource_id) — and carries no inherent tenancy. "Account in
org B may use agent in org A" is therefore perfectly expressible, and if such
a row existed the agent would surface in B's list through the "or granted"
arm. The grant table would become a documented way around org_id, which is
the one thing the tenancy boundary cannot have.

Two independent defences, tested separately because they fail differently:

  WRITE side  assert_same_org refuses to create the row.
  READ side   accessible_resource_ids confines resolution to the caller's org,
              so a row that already exists (written before the check landed,
              or by a script) still cannot be used.

Neither alone is sufficient. The write check does nothing about history; the
read filter does nothing about a row stamped with the principal's org rather
than the resource's.
"""
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import AccessGrant, Agent
from src.services import access
from tests.org_isolation import client_for, make_tenant

AGENTS_URL = "/api/agents/"


@pytest.fixture()
def acme(db: Session):
    return make_tenant(db, slug="xorg-acme", account_id=5501, data_scope="shared")


@pytest.fixture()
def beta(db: Session):
    return make_tenant(db, slug="xorg-beta", account_id=5502, data_scope="shared")


def _agent(t, name="Secret"):
    return Agent(org_id=t.org_id, account_id=t.account_id, name=name,
                 visibility="private", config={"data": {}})


# ---------------------------------------------------------------------------
# read side
# ---------------------------------------------------------------------------

async def test_preexisting_cross_org_grant_does_not_leak(
    db: Session, _override_db, acme, beta
):
    """The row is inserted directly, bypassing the API — modelling a grant
    written before the same-org check existed."""
    secret = _agent(acme); db.add(secret); db.flush()
    db.add(AccessGrant(org_id=acme.org_id, principal_type="account",
                       principal_id=beta.account_id, resource_type="agent",
                       resource_id=secret.id, role="use"))
    db.flush()

    async with client_for(beta) as c:
        resp = await c.get(AGENTS_URL)
    assert resp.status_code == 200
    assert secret.id not in {a["id"] for a in resp.json()}, (
        "CROSS-TENANT LEAK: a stale cross-org grant surfaced another org's agent")


async def test_grant_within_the_same_org_still_works(db: Session, _override_db, acme):
    """The confinement must not break legitimate intra-org sharing — otherwise
    the leak test above would pass simply because nothing resolves at all."""
    colleague = make_tenant(db, slug="xorg-acme", account_id=5503, data_scope="shared")
    private = _agent(acme, "Private To Owner")
    db.add(private); db.flush()

    async with client_for(colleague) as c:
        before = {a["id"] for a in (await c.get(AGENTS_URL)).json()}
    assert private.id not in before

    db.add(AccessGrant(org_id=acme.org_id, principal_type="account",
                       principal_id=colleague.account_id, resource_type="agent",
                       resource_id=private.id, role="use"))
    db.flush()

    async with client_for(colleague) as c:
        after = {a["id"] for a in (await c.get(AGENTS_URL)).json()}
    assert private.id in after, "an intra-org grant must still grant access"


# ---------------------------------------------------------------------------
# write side
# ---------------------------------------------------------------------------

def test_assert_same_org_rejects_a_foreign_resource(db: Session, acme, beta):
    secret = _agent(acme); db.add(secret); db.flush()

    with pytest.raises(access.CrossOrgGrantError, match="belongs to org"):
        access.assert_same_org(db, beta.org_id, "account", beta.account_id,
                               "agent", secret.id)


def test_assert_same_org_rejects_a_non_member_principal(db: Session, acme, beta):
    ours = _agent(acme); db.add(ours); db.flush()

    with pytest.raises(access.CrossOrgGrantError, match="not a member"):
        access.assert_same_org(db, acme.org_id, "account", beta.account_id,
                               "agent", ours.id)


def test_a_grant_can_no_longer_name_anything_but_an_account(db: Session, acme):
    """The group principal is gone, and the CHECK is what keeps it gone.

    While `principal_type` admitted 'group', assert_same_org had to reason
    about a second kind of principal with its own org — including groups
    predating org scoping, whose NULL org had to be treated as "unusable"
    rather than "matches". Groups became spaces, whose audience deliberately
    crossed orgs, which is why it lived in space_resources
    instead of here. This asserts the column cannot quietly grow the arm back.
    """
    ours = _agent(acme); db.add(ours); db.flush()

    db.add(AccessGrant(org_id=acme.org_id, principal_type="group",
                       principal_id=1, resource_type="agent",
                       resource_id=ours.id, role="use"))
    with pytest.raises(IntegrityError, match="ck_access_grant_principal_type"):
        db.flush()
    db.rollback()


def test_assert_same_org_accepts_a_valid_intra_org_grant(db: Session, acme):
    colleague = make_tenant(db, slug="xorg-acme", account_id=5504, data_scope="shared")
    ours = _agent(acme); db.add(ours); db.flush()

    access.assert_same_org(db, acme.org_id, "account", colleague.account_id,
                           "agent", ours.id)   # must not raise


async def test_api_refuses_to_create_a_cross_org_grant(
    db: Session, _override_db, acme, beta
):
    """End to end: the endpoint returns 404, not 403.

    Confirming that a resource exists in another org is itself a small leak,
    so the refusal is indistinguishable from 'no such agent'.
    """
    secret = _agent(acme); db.add(secret); db.flush()

    async with client_for(beta) as c:
        resp = await c.post(f"/api/agents/{secret.id}/access-grants",
                            json={"granteeEmail": beta.account.email})
    assert resp.status_code == 404, resp.text
    assert db.query(AccessGrant).filter(
        AccessGrant.resource_id == secret.id).count() == 0
