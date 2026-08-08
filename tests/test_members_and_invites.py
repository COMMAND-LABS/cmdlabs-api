"""
Invites, the switcher, and the platform-admin browsers.

The rules worth pinning here are the ones that are cheap to break and
expensive to notice: an invited member must not depend on a subscription they
never bought, an org must never end up with no owner, a removal must bite on
the next request, and the admin surface must stay administration rather than
quiet read access.
"""
import contextlib

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.db.models import (
    Account,
    Contact,
    Organization,
    OrganizationInvitation,
    OrganizationMember,
)
from tests.conftest import ROOT_ORG_ID, make_token
from tests.org_isolation import Tenant, client_for, make_tenant

MEMBERS = "/api/organizations/members"
MINE = "/api/organizations/mine"
INVITATIONS = "/api/organizations/invitations"


@pytest.fixture()
def team(db: Session):
    """An owner with a named org and both tiers."""
    t = make_tenant(db, slug="invite-co", account_id=9601, role="manager",
                    is_owner=True)
    return t


# The RAW token of the most recent invitation, captured on its way to the
# mailer. It is unrecoverable from the row by design — only the sha256 is
# stored — so a test that wants to follow the emailed link has to read it here,
# which is exactly the property being asserted.
_last_token: str | None = None


@pytest.fixture(autouse=True)
def _capture_invitation_email(monkeypatch):
    """Intercept the invitation mail, and remember its token.

    Also keeps the suite off SES: send_invitation queues a background task that
    would otherwise try to build a boto3 client.
    """
    def _fake(db, background_tasks, invitation, token):
        global _last_token
        _last_token = token

    monkeypatch.setattr(
        "src.routers.organizations.members.send_invitation", _fake)
    global _last_token
    _last_token = None
    yield


def _account(db: Session, account_id: int, email: str) -> Account:
    """A bare account with NO org — what an invitee is before they answer."""
    acct = Account(id=account_id, email=email)
    db.add(acct)
    db.flush()
    return acct


@contextlib.asynccontextmanager
async def _client_as(account: Account):
    """A client authenticated as one account, org or no org.

    org_isolation.client_for takes a Tenant, which presupposes a membership.
    The whole point of an invitee is that they do not have one yet.
    """
    from src.main import app

    token = make_token(email=account.email, user_id=account.id)
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


def _invitation_for(db: Session, email: str) -> OrganizationInvitation:
    return (db.query(OrganizationInvitation)
              .filter(OrganizationInvitation.email == email.lower())
              .order_by(OrganizationInvitation.id.desc()).first())


# ---------------------------------------------------------------------------
# naming a workspace
# ---------------------------------------------------------------------------

async def test_invite_grants_nothing_until_it_is_accepted(
    db: Session, _override_db, team
):
    """THE RULE THE WHOLE INVITATION FLOW EXISTS FOR.

    This test used to be called "invite creates an account and grants access"
    and asserted exactly that: an invite INSERTed an Account for an address
    that had proved nothing, wrote the membership immediately, and mailed the
    ordinary sign-in code. Both halves were wrong, and this pins the reversal.

      no account   a typo'd invite left a permanent account row nobody
                   controlled. An invitation names an ADDRESS; the account is
                   created when its owner shows up at /request-code.

      no member    being added to somebody's tenant without being asked is the
                   consent gap routers/organizations/members.py documented for
                   months. The membership is written on accept.
    """
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": "New.Person@Acme.test",
                                           "role": "community_member"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == "new.person@acme.test", "normalized"
    assert body["role"] == "community_member"

    assert db.query(Account).filter(
        Account.email == "new.person@acme.test").first() is None, (
        "inviting must not mint an account for an address that has proved "
        "nothing")

    invitation = db.query(OrganizationInvitation).filter(
        OrganizationInvitation.email == "new.person@acme.test").one()
    assert invitation.org_id == team.org_id
    assert invitation.accepted_at is None
    assert db.query(OrganizationMember).filter(
        OrganizationMember.org_id == team.org_id).count() == 1, (
        "still just the owner — an offer is not a membership")


async def test_accepting_is_what_creates_the_membership(
    db: Session, _override_db, team
):
    """And it rides on the ORG, never on a subscription they never bought."""
    invitee = _account(db, 9620, "joins@acme.test")

    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": invitee.email,
                                    "role": "community_member"})
    invitation = _invitation_for(db, invitee.email)

    async with _client_as(invitee) as c:
        resp = await c.post(f"{INVITATIONS}/{invitation.id}/accept")
    assert resp.status_code == 200, resp.text
    assert resp.json()["org_id"] == team.org_id

    member = db.query(OrganizationMember).filter(
        OrganizationMember.org_id == team.org_id,
        OrganizationMember.account_id == invitee.id).one()
    assert member.granted_by == "grant", (
        "an invited member's access rides on the org, never on a subscription "
        "they were never asked to buy")
    db.refresh(invitee)
    assert invitee.default_org_id == team.org_id, (
        "somebody with no home lands where they were invited")


async def test_an_existing_account_keeps_its_own_default_org(
    db: Session, _override_db, team
):
    """Being invited must not move somebody out of their own workspace.

    Now asserted across the ACCEPT as well as the invite: joining a second org
    is not a reason to re-point somebody's dashboard at it.
    """
    other = make_tenant(db, slug="already-here", account_id=9604)
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": other.account.email,
                                           "role": "community_member"})
    assert resp.status_code == 201

    invitation = _invitation_for(db, other.account.email)
    async with _client_as(other.account) as c:
        assert (await c.post(
            f"{INVITATIONS}/{invitation.id}/accept")).status_code == 200

    db.refresh(other.account)
    assert other.account.default_org_id == other.org_id


async def test_only_the_invited_address_can_accept(
    db: Session, _override_db, team
):
    """The token says WHICH invitation. The session says who is answering.

    A forwarded link is the case: it shows a stranger the page and gets them
    nothing. 403 rather than 404 on purpose — they are holding a real link, and
    the answer they need is "sign in as the other address", not "no such page".
    """
    stranger = make_tenant(db, slug="stranger-home", account_id=9621)
    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "someone.else@acme.test",
                                    "role": "community_member"})
    invitation = _invitation_for(db, "someone.else@acme.test")

    async with client_for(stranger) as c:
        resp = await c.post(f"{INVITATIONS}/{invitation.id}/accept")
    assert resp.status_code == 403
    assert "someone.else@acme.test" in resp.json()["detail"]
    assert db.query(OrganizationMember).filter(
        OrganizationMember.org_id == team.org_id,
        OrganizationMember.account_id == stranger.account_id).first() is None


async def test_a_revoked_invitation_cannot_be_accepted(
    db: Session, _override_db, team
):
    """Takes effect on their next click — there is no email to unsend."""
    invitee = _account(db, 9622, "too-late@acme.test")
    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": invitee.email,
                                    "role": "community_member"})
        invitation = _invitation_for(db, invitee.email)
        gone = await c.delete(f"{INVITATIONS}/{invitation.id}")
    assert gone.status_code == 204

    async with _client_as(invitee) as c:
        resp = await c.post(f"{INVITATIONS}/{invitation.id}/accept")
    assert resp.status_code == 409


async def test_the_public_lookup_leaks_nothing_about_the_org(
    db: Session, _override_db, team
):
    """Served without authentication, so it may carry only what the invitation
    email already told them: who invited them, where, and as what.

    Not the roster, not the plan, not the member count. This asserts the one
    that would be easiest to add by accident.
    """
    from src.services import invitations as invitations_service

    db.add(Contact(org_id=team.org_id, account_id=team.account_id,
                   first_name="Very", last_name="Secret",
                   email="roster-secret@invite.test"))
    db.flush()

    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "reader@acme.test",
                                    "role": "community_member"})
    token = _last_token
    assert token, "issue() handed back a raw token"

    # No Authorization header at all — this is the signed-out visitor.
    from httpx import ASGITransport, AsyncClient
    from src.main import app
    async with AsyncClient(transport=ASGITransport(app=app),
                           base_url="http://test") as c:
        resp = await c.get(f"{INVITATIONS}/{token}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["email"] == "reader@acme.test"
    assert body["status"] == "pending"
    assert "roster-secret@invite.test" not in resp.text
    assert "plan" not in body and "members" not in body

    # And the stored form is a hash, not the token.
    invitation = _invitation_for(db, "reader@acme.test")
    assert invitation.token_hash != token
    assert invitations_service.find_by_token(db, token).id == invitation.id


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


async def test_inviting_twice_refreshes_rather_than_duplicating(
    db: Session, _override_db, team
):
    """Two LIVE invitations to one address is how somebody clicks the dead one.

    This used to assert 409, back when the first invite added them outright and
    the second collided with the membership. Re-inviting is now the ordinary
    way to fix a mis-picked role or revive an expired offer, so it refreshes
    the row in place — new token, new expiry, same invitation.
    """
    async with client_for(team) as c:
        first = await c.post(MEMBERS, json={"email": "dup@y.test",
                                            "role": "community_member"})
        first_token = _last_token
        again = await c.post(MEMBERS, json={"email": "dup@y.test",
                                            "role": "manager"})
    assert first.status_code == 201 and again.status_code == 201
    assert first.json()["id"] == again.json()["id"], "one row, not two"
    assert again.json()["role"] == "manager", "the newer role wins"
    assert _last_token != first_token, (
        "a fresh token, so the link in the first email stops working")

    assert db.query(OrganizationInvitation).filter(
        OrganizationInvitation.org_id == team.org_id,
        OrganizationInvitation.email == "dup@y.test").count() == 1


async def test_inviting_somebody_already_in_the_org_is_a_conflict(
    db: Session, _override_db, team
):
    colleague = make_tenant(db, slug="invite-co", account_id=9612,
                            role="manager", is_owner=False)
    async with client_for(team) as c:
        resp = await c.post(MEMBERS, json={"email": colleague.account.email,
                                           "role": "community_member"})
    assert resp.status_code == 409


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


# ---------------------------------------------------------------------------
# no workspace-per-invitee
# ---------------------------------------------------------------------------
#
# The bug these pin, in the order it happened:
#
#   20:17  the owner invites ceemmmdee@gmail.com to CMD LABS
#   20:19  they sign in — and ensure_membership creates org 4, 'ceemmmdee',
#          owned by them, because it only ever asked "do they own an org?"
#
# They ended up owning an empty organization named after their email address,
# on top of the team they had actually been invited to.


async def test_signing_in_with_an_invitation_waiting_creates_no_workspace(
    db: Session, _override_db, team
):
    from src.services import organizations

    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "waiting@acme.test",
                                    "role": "community_member"})

    invitee = _account(db, 9630, "waiting@acme.test")
    assert organizations.ensure_membership(db, invitee) is None, (
        "they have somewhere to go — minting a second home is the bug")

    assert db.query(Organization).filter(
        Organization.owner_account_id == invitee.id).first() is None
    assert db.query(OrganizationMember).filter(
        OrganizationMember.account_id == invitee.id).count() == 0


async def test_declining_is_what_gives_them_a_workspace(
    db: Session, _override_db, team
):
    """The other half, and it is not optional.

    Nothing is created while the invitation is live, so somebody who signs in
    only to say no would be left with no org and a 403 on every screen.
    Declining is the moment that stops being true.
    """
    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "nothanks@acme.test",
                                    "role": "community_member"})
    invitee = _account(db, 9631, "nothanks@acme.test")
    invitation = _invitation_for(db, invitee.email)

    async with _client_as(invitee) as c:
        resp = await c.post(f"{INVITATIONS}/{invitation.id}/decline")
    assert resp.status_code == 204

    db.refresh(invitation)
    assert invitation.declined_at is not None
    own = db.query(Organization).filter(
        Organization.owner_account_id == invitee.id).one()
    assert db.query(OrganizationMember).filter(
        OrganizationMember.account_id == invitee.id,
        OrganizationMember.org_id == own.id).count() == 1


async def test_an_ordinary_signup_still_gets_a_workspace(
    db: Session, _override_db
):
    """The fix must not withhold a workspace from somebody who needs one."""
    from src.services import organizations

    fresh = _account(db, 9632, "solo@acme.test")
    member = organizations.ensure_membership(db, fresh)
    assert member is not None
    org = db.query(Organization).filter(
        Organization.owner_account_id == fresh.id).one()
    assert member.org_id == org.id
    db.refresh(fresh)
    assert fresh.default_org_id == org.id


async def test_accepting_leaves_them_with_exactly_one_membership(
    db: Session, _override_db, team
):
    """The end-to-end shape of the fixed flow: invited, signs in, accepts, and
    is a member of ONE org — the one they were invited to."""
    from src.services import organizations

    async with client_for(team) as c:
        await c.post(MEMBERS, json={"email": "clean@acme.test",
                                    "role": "community_member"})
    invitee = _account(db, 9633, "clean@acme.test")
    organizations.ensure_membership(db, invitee)   # their first verified login

    invitation = _invitation_for(db, invitee.email)
    async with _client_as(invitee) as c:
        assert (await c.post(
            f"{INVITATIONS}/{invitation.id}/accept")).status_code == 200

    rows = db.query(OrganizationMember).filter(
        OrganizationMember.account_id == invitee.id).all()
    assert [r.org_id for r in rows] == [team.org_id]
    assert db.query(Organization).filter(
        Organization.owner_account_id == invitee.id).first() is None, (
        "no empty org named after their email address")


# ---------------------------------------------------------------------------
# the stale org cookie
# ---------------------------------------------------------------------------


async def test_a_cookie_for_an_org_you_are_not_in_falls_back(
    db: Session, _override_db, team
):
    """What locked out the account in the bug report.

    The cookie is written by client JS with a one-year max-age and used to be
    cleared by nothing, so it outlived both the session and the membership.
    get_org_context refused it outright — which meant EVERY org-scoped route
    403'd, including /organizations/mine, so the switcher that would have fixed
    it could not render. A failure nobody can act on is not feedback.

    Enforcement is unchanged: the fallback target is the account's own
    default_org_id, a value only the server writes.
    """
    from src.deps import ORG_COOKIE_NAME

    outsider = make_tenant(db, slug="somewhere-else", account_id=9640)

    async with client_for(outsider) as c:
        resp = await c.get(MINE, cookies={ORG_COOKIE_NAME: str(team.org_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["active_org_id"] == outsider.org_id, (
        "ignored, not fatal — they land in their own org")
    assert {o["id"] for o in body["organizations"]} == {outsider.org_id}


async def test_the_cookie_still_chooses_among_orgs_you_are_in(
    db: Session, _override_db, team
):
    """The fallback must not have turned the switcher off."""
    from src.deps import ORG_COOKIE_NAME

    joiner = make_tenant(db, slug="joiner-two", account_id=9641)
    db.add(OrganizationMember(org_id=team.org_id, account_id=joiner.account_id,
                              role="manager", granted_by="grant"))
    db.flush()

    async with client_for(joiner) as c:
        body = (await c.get(MINE,
                            cookies={ORG_COOKIE_NAME: str(team.org_id)})).json()
    assert body["active_org_id"] == team.org_id
