"""
Invites, the switcher, and the platform-admin browsers.

The rules worth pinning here are the ones that are cheap to break and
expensive to notice: an invited member must not depend on a subscription they
never bought, an org must never end up with no owner, a removal must bite on
the next request, and the admin surface must stay administration rather than
quiet read access.
"""
import pytest
from sqlalchemy.orm import Session

from src.db.models import (
    Account,
    Contact,
    Organization,
    OrganizationMember,
)
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import Tenant, client_for, make_tenant

MEMBERS = "/api/organizations/members"
MINE = "/api/organizations/mine"


@pytest.fixture()
def team(db: Session):
    """An owner with a named org and both tiers."""
    t = make_tenant(db, slug="invite-co", account_id=9601, role="manager",
                    is_owner=True)
    return t


# ---------------------------------------------------------------------------
# naming a workspace
# ---------------------------------------------------------------------------

async def test_invite_creates_an_account_and_grants_access(
    db: Session, _override_db, team
):
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": "New.Person@Acme.test",
                                           "role": "community_member"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "new.person@acme.test", "normalized"
    assert body["granted_by"] == "grant", (
        "an invited member's access rides on the org, never on a subscription "
        "they were never asked to buy")

    acct = db.query(Account).filter(Account.email == "new.person@acme.test").one()
    assert acct.default_org_id == team.org_id, "a new account lands where invited"


async def test_an_existing_account_keeps_its_own_default_org(
    db: Session, _override_db, team
):
    """Being invited must not move somebody out of their own workspace."""
    other = make_tenant(db, slug="already-here", account_id=9604)
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": other.account.email,
                                           "role": "community_member"})
    assert resp.status_code == 201
    db.refresh(other.account)
    assert other.account.default_org_id == other.org_id


async def test_invite_refuses_an_unknown_role(db: Session, _override_db, team):
    """Otherwise the member resolves to no modules and it reads as a
    permissions bug rather than a typo.

    This used to say "refuses a tier from another org" — tier keys were per-org,
    so naming a real tier belonging to somebody ELSE was the realistic mistake.
    Roles are platform-wide, so the only way to get this wrong now is a typo,
    which is a smaller failure mode and is what this pins.

    422 rather than letting ck_org_member_role turn it into a 500: the database
    would refuse it either way, and the caller deserves to be told which field
    was wrong.
    """
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": "z@y.test",
                                           "role": "not_a_role"})
    assert resp.status_code == 422


async def test_only_an_owner_can_invite(db: Session, _override_db, team):
    plain = make_tenant(db, slug="invite-co", account_id=9605,
                        role="manager", is_owner=False)
    async with client_for(plain) as c:
        resp = await c.post(MEMBERS, json={"email": "n@y.test",
                                           "role": "community_member"})
    assert resp.status_code == 404, "the admin surface does not confirm it exists"


async def test_inviting_twice_is_a_conflict(db: Session, _override_db, team):
    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "dup@y.test", "role": "community_member"})
        again = await c.post(MEMBERS, json={"email": "dup@y.test",
                                            "role": "community_member"})
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# removal
# ---------------------------------------------------------------------------

async def test_removal_bites_on_the_very_next_request(
    db: Session, _override_db, team
):
    """No token to revoke, no cache to invalidate — get_org_context re-checks
    membership every time."""
    colleague = make_tenant(db, slug="invite-co", account_id=9606,
                            role="manager", is_owner=False)
    db.add(Contact(org_id=team.org_id, account_id=team.account_id,
                   first_name="A", last_name="B", email="c@invite.test"))
    db.flush()

    async with client_for(colleague) as c:
        assert (await c.get("/api/contacts/")).status_code == 200

    async with client_for(team) as c:
        gone = await c.delete(f"{MEMBERS}/{colleague.account_id}")
    assert gone.status_code == 204

    async with client_for(colleague) as c:
        assert (await c.get("/api/contacts/")).status_code == 403


async def test_the_owner_cannot_be_removed(db: Session, _override_db, team):
    """An owner outside their own org can invite nobody, set no tiers, and hand
    it over to no one — it would need a super admin to become usable again.

    Was "the LAST owner", which only sounded plural because ownership used to
    be a flag on each membership row. An org names exactly one owner, so there
    is never a second to fall back on and the check is an equality rather than
    a count.
    """
    async with client_for(team) as c:
        resp = await c.delete(f"{MEMBERS}/{team.account_id}")
    assert resp.status_code == 409
    assert "owner" in resp.json()["detail"].lower()


async def test_a_removed_member_keeps_their_authored_rows(
    db: Session, _override_db, team
):
    """Attribution outlives membership. Deleting a departing colleague's work
    would be an unrecoverable answer to a reversible problem."""
    colleague = make_tenant(db, slug="invite-co", account_id=9607,
                            role="manager", is_owner=False)
    row = Contact(org_id=team.org_id, account_id=colleague.account_id,
                  first_name="Theirs", last_name="X", email="t@invite.test")
    db.add(row); db.flush()
    row_id = row.id

    async with client_for(team) as c:
        await c.delete(f"{MEMBERS}/{colleague.account_id}")

    kept = db.query(Contact).filter(Contact.id == row_id).one()
    assert kept.account_id == colleague.account_id


# ---------------------------------------------------------------------------
# the switcher
# ---------------------------------------------------------------------------

async def test_the_switcher_lists_only_orgs_you_belong_to(
    db: Session, _override_db, team
):
    """If it could list one you were removed from, you would pick it and get a
    403 you could not explain."""
    make_tenant(db, slug="not-mine", account_id=9608)
    async with client_for(team) as c:
        resp = await c.get(MINE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["active_org_id"] == team.org_id
    assert {o["id"] for o in body["organizations"]} == {team.org_id}


async def test_the_switcher_reflects_a_new_membership(
    db: Session, _override_db, team
):
    joiner = make_tenant(db, slug="joiner-home", account_id=9609)
    db.add(OrganizationMember(org_id=team.org_id, account_id=joiner.account_id,
                              role="manager", granted_by="grant"))
    db.flush()

    async with client_for(joiner) as c:
        body = (await c.get(MINE)).json()
    assert {o["id"] for o in body["organizations"]} == {joiner.org_id, team.org_id}


# ---------------------------------------------------------------------------
# platform-admin browsers
# ---------------------------------------------------------------------------

@pytest.fixture()
def super_admin_client_and_org(db: Session):
    super_admin = Account(id=9700, email="superadmin2@cmdlabs.io", is_super_admin=True,
                    default_org_id=ROOT_ORG_ID)
    db.add(super_admin); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=super_admin.id,
                              role="manager", granted_by="grant"))
    db.flush()
    return super_admin


async def test_admin_browser_carries_no_tenant_data(
    db: Session, _override_db, super_admin_client_and_org
):
    """Administration, not read access. Super admins reach an org's rows by
    JOINING it, which leaves a membership row its members can see."""
    from httpx import ASGITransport, AsyncClient
    from src.main import app

    acme = make_tenant(db, slug="admin-nodata", account_id=9702)
    db.add(Contact(org_id=acme.org_id, account_id=acme.account_id,
                   first_name="Very", last_name="Secret",
                   email="secret@nodata.test"))
    db.flush()

    token = make_token(email=super_admin_client_and_org.email,
                       user_id=super_admin_client_and_org.id)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        body = (await c.get(f"/api/admin/organizations/{acme.org_id}")).text

    assert "secret@nodata.test" not in body
    assert "Secret" not in body


async def test_a_non_super_admin_account_cannot_reach_the_browsers(
    db: Session, _override_db, team
):
    async with client_for(team) as c:
        assert (await c.get(
            f"/api/admin/organizations/{team.org_id}")).status_code == 404
        assert (await c.get("/api/admin/groups")).status_code == 404


# ---------------------------------------------------------------------------
# viewing and renaming
# ---------------------------------------------------------------------------

async def test_renaming_is_audited(db: Session, _override_db, team):
    """The log snapshots names at write time so history survives a rename —
    which only reads correctly if the rename itself is recorded."""
    from src.db.models import AccessGrantEvent
    from src.services import audit

    async with client_for(team) as c:
        await c.put("/api/organizations/name", json={"name": "Renamed Co"})

    ev = (db.query(AccessGrantEvent)
            .filter(AccessGrantEvent.event_type == audit.ORG_RENAME,
                    AccessGrantEvent.org_id == team.org_id).one())
    assert "Renamed Co" in ev.detail
    assert ev.actor_account_id == team.account_id


async def test_a_blank_name_is_refused(db: Session, _override_db, team):
    async with client_for(team) as c:
        resp = await c.put("/api/organizations/name", json={"name": "   "})
    assert resp.status_code == 422


async def test_a_member_cannot_rename(db: Session, _override_db, team):
    plain = make_tenant(db, slug="invite-co", account_id=9611,
                        role="manager", is_owner=False)
    async with client_for(plain) as c:
        resp = await c.put("/api/organizations/name", json={"name": "Hijack"})
    assert resp.status_code == 404
