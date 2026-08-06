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
    AccessGroup,
    AccessGroupMember,
    Account,
    Contact,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import Tenant, client_for, make_tenant

MEMBERS = "/api/organizations/members"
MINE = "/api/organizations/mine"


@pytest.fixture()
def team(db: Session):
    """An owner with a named org and both tiers."""
    t = make_tenant(db, slug="invite-co", account_id=9601, tier_key="owner",
                    is_owner=True)
    if not db.query(OrganizationTier).filter(
            OrganizationTier.org_id == t.org_id,
            OrganizationTier.tier_key == "member").first():
        db.add(OrganizationTier(org_id=t.org_id, tier_key="member",
                                label="Member", modules=["home", "contacts"]))
        db.flush()
    return t


# ---------------------------------------------------------------------------
# naming a workspace
# ---------------------------------------------------------------------------

async def test_a_workspace_must_be_named_before_inviting(db: Session, _override_db):
    """A team is a thing with a name — it shows up in a switcher and an audit
    log, so it gets one deliberately rather than auto-generated."""
    solo = make_tenant(db, slug="unnamed-co", account_id=9602, tier_key="owner",
                       is_owner=True)
    org = db.query(Organization).filter(Organization.id == solo.org_id).one()
    org.slug = None
    db.flush()

    async with client_for(solo) as c:
        resp = await c.post(MEMBERS, json={"email": "x@y.test",
                                           "tier_key": "member"})
    assert resp.status_code == 409
    assert "name your organization" in resp.json()["detail"].lower()


async def test_a_slug_cannot_be_changed_once_set(db: Session, _override_db, team):
    async with client_for(team) as c:
        resp = await c.put("/api/organizations/slug", json={"slug": "something-else"})
    assert resp.status_code == 409


async def test_reserved_and_malformed_slugs_are_refused(db: Session, _override_db):
    solo = make_tenant(db, slug="tbn-co", account_id=9603, tier_key="owner",
                       is_owner=True)
    org = db.query(Organization).filter(Organization.id == solo.org_id).one()
    org.slug = None
    db.flush()

    async with client_for(solo) as c:
        assert (await c.put("/api/organizations/slug",
                            json={"slug": "cmdlabs"})).status_code == 409
        assert (await c.put("/api/organizations/slug",
                            json={"slug": "Has Spaces"})).status_code == 422
        ok = await c.put("/api/organizations/slug", json={"slug": "TidyCo"})
    assert ok.status_code == 200
    assert ok.json()["org_slug"] == "tidyco", "normalized to lowercase"


# ---------------------------------------------------------------------------
# inviting
# ---------------------------------------------------------------------------

async def test_invite_creates_an_account_and_grants_access(
    db: Session, _override_db, team
):
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": "New.Person@Acme.test",
                                           "tier_key": "member"})
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
                                           "tier_key": "member"})
    assert resp.status_code == 201
    db.refresh(other.account)
    assert other.account.default_org_id == other.org_id


async def test_invite_refuses_a_tier_from_another_org(
    db: Session, _override_db, team
):
    """Otherwise the member resolves to no modules and it reads as a
    permissions bug rather than a typo."""
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": "z@y.test",
                                           "tier_key": "premium"})
    assert resp.status_code == 422


async def test_only_an_owner_can_invite(db: Session, _override_db, team):
    plain = make_tenant(db, slug="invite-co", account_id=9605,
                        tier_key="member", is_owner=False)
    async with client_for(plain) as c:
        resp = await c.post(MEMBERS, json={"email": "n@y.test",
                                           "tier_key": "member"})
    assert resp.status_code == 404, "the admin surface does not confirm it exists"


async def test_inviting_twice_is_a_conflict(db: Session, _override_db, team):
    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "dup@y.test", "tier_key": "member"})
        again = await c.post(MEMBERS, json={"email": "dup@y.test",
                                            "tier_key": "member"})
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
                            tier_key="member", is_owner=False)
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


async def test_the_last_owner_cannot_be_removed(db: Session, _override_db, team):
    """An org with no owner has nobody who can invite, set tiers, or hand it
    over — it would need staff to become usable again."""
    async with client_for(team) as c:
        resp = await c.delete(f"{MEMBERS}/{team.account_id}")
    assert resp.status_code == 409
    assert "only owner" in resp.json()["detail"].lower()


async def test_a_removed_member_keeps_their_authored_rows(
    db: Session, _override_db, team
):
    """Attribution outlives membership. Deleting a departing colleague's work
    would be an unrecoverable answer to a reversible problem."""
    colleague = make_tenant(db, slug="invite-co", account_id=9607,
                            tier_key="member", is_owner=False)
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
                              tier_key="member", granted_by="grant",
                              is_owner=False))
    db.flush()

    async with client_for(joiner) as c:
        body = (await c.get(MINE)).json()
    assert {o["id"] for o in body["organizations"]} == {joiner.org_id, team.org_id}


# ---------------------------------------------------------------------------
# platform-admin browsers
# ---------------------------------------------------------------------------

@pytest.fixture()
def staff_client_and_org(db: Session):
    staff = Account(id=9700, email="staff2@cmdlabs.io", role="admin",
                    default_org_id=ROOT_ORG_ID)
    db.add(staff); db.flush()
    db.add(OrganizationMember(org_id=ROOT_ORG_ID, account_id=staff.id,
                              tier_key="org_owner", granted_by="grant",
                              is_owner=True))
    db.flush()
    return staff


async def test_admin_sees_members_groups_and_effective_modules(
    db: Session, _override_db, staff_client_and_org
):
    """The support question is 'why can't they see X', and a tier name alone
    does not answer it — the intersection does."""
    from httpx import ASGITransport, AsyncClient
    from src.main import app

    acme = make_tenant(db, slug="admin-browse", account_id=9701,
                       tier_key="member", is_owner=False)
    org = db.query(Organization).filter(Organization.id == acme.org_id).one()
    org.granted_modules = ["home", "contacts"]
    tier = db.query(OrganizationTier).filter(
        OrganizationTier.org_id == acme.org_id).first()
    tier.modules = ["contacts", "deals"]      # deals is outside the ceiling
    group = AccessGroup(org_id=acme.org_id, name="Sales",
                        owner_account_id=acme.account_id)
    db.add(group); db.flush()
    db.add(AccessGroupMember(access_group_id=group.id,
                             account_id=acme.account_id, role="admin"))
    db.flush()

    token = make_token(email=staff_client_and_org.email,
                       user_id=staff_client_and_org.id)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        resp = await c.get(f"/api/admin/organizations/{acme.org_id}")
        groups = await c.get("/api/admin/groups")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["email"] for m in body["members"]] == [acme.account.email]
    assert body["members"][0]["effective_modules"] == ["contacts"], (
        "ceiling ∩ tier, shown resolved")
    assert body["groups"][0]["name"] == "Sales"
    assert body["groups"][0]["members"][0]["in_org"] is True

    assert groups.status_code == 200
    listed = {g["name"]: g for g in groups.json()}
    assert listed["Sales"]["org_slug"] == "admin-browse"


async def test_admin_browser_carries_no_tenant_data(
    db: Session, _override_db, staff_client_and_org
):
    """Administration, not read access. Staff reach an org's rows by JOINING
    it, which leaves a membership row its members can see."""
    from httpx import ASGITransport, AsyncClient
    from src.main import app

    acme = make_tenant(db, slug="admin-nodata", account_id=9702)
    db.add(Contact(org_id=acme.org_id, account_id=acme.account_id,
                   first_name="Very", last_name="Secret",
                   email="secret@nodata.test"))
    db.flush()

    token = make_token(email=staff_client_and_org.email,
                       user_id=staff_client_and_org.id)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        body = (await c.get(f"/api/admin/organizations/{acme.org_id}")).text

    assert "secret@nodata.test" not in body
    assert "Secret" not in body


async def test_a_non_staff_account_cannot_reach_the_browsers(
    db: Session, _override_db, team
):
    async with client_for(team) as c:
        assert (await c.get(
            f"/api/admin/organizations/{team.org_id}")).status_code == 404
        assert (await c.get("/api/admin/groups")).status_code == 404


# ---------------------------------------------------------------------------
# viewing and renaming
# ---------------------------------------------------------------------------

async def test_any_member_can_see_the_org_details(db: Session, _override_db, team):
    """Knowing which tenant your records live in is not privileged. The
    switcher shows only a name, so this is where the rest lives."""
    plain = make_tenant(db, slug="invite-co", account_id=9610,
                        tier_key="member", is_owner=False)
    async with client_for(plain) as c:
        body = (await c.get(MEMBERS)).json()
    assert body["org_name"] == "Invite-Co"
    assert body["org_slug"] == "invite-co"
    assert body["can_manage"] is False


async def test_an_owner_can_rename_the_display_name(db: Session, _override_db, team):
    """The API promised this in the 409 from /slug before anything did it."""
    async with client_for(team) as c:
        resp = await c.put("/api/organizations/name", json={"name": "Acme Rebrand"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["org_name"] == "Acme Rebrand"
    assert body["org_slug"] == "invite-co", "the identifier does not move"


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
                        tier_key="member", is_owner=False)
    async with client_for(plain) as c:
        resp = await c.put("/api/organizations/name", json={"name": "Hijack"})
    assert resp.status_code == 404
