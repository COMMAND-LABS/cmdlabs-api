"""
Organization public addresses (slugs), and signing in through one.

An org MAY carry a slug so that /org/<slug>/login can show its name and land
a member in it. Three things are pinned here:

  the address is the OWNER'S to set, validated with a reason the owner can
  act on, and unique across orgs;

  the public lookup discloses a name and nothing else — it renders for people
  who have not signed in;

  signing in through an org's page decides where the session LANDS and never
  whether it belongs: members are sent in (and it becomes their default),
  everyone else is told so, and nobody is joined to anything.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session

from src.config.roles_registry import ROLE_COMMUNITY_MEMBER, ROLE_MANAGER
from src.db.models import (
    AccessGrantEvent,
    Account,
    Organization,
    OrganizationMember,
)
from src.routers.auth.router import _hash_otp
from src.services import audit
from src.services.organizations import own_org_for
from tests.org_isolation import client_for, make_tenant

VERIFY = "/api/auth/verify-code"
BY_SLUG = "/api/organizations/by-slug/{slug}"


def _slug_url(tenant) -> str:
    return f"/api/organizations/{tenant.org_id}/slug"


@pytest.fixture()
def acme(db: Session):
    """An org and its owner."""
    return make_tenant(db, slug="slug-acme", account_id=9701,
                       role=ROLE_MANAGER, is_owner=True)


@pytest.fixture()
def colleague(db: Session, acme):
    """A plain member of the SAME org."""
    return make_tenant(db, slug="slug-acme", account_id=9702,
                       role=ROLE_COMMUNITY_MEMBER, is_owner=False)


@pytest.fixture()
def stranger(db: Session):
    """The owner of an unrelated org."""
    return make_tenant(db, slug="slug-beta", account_id=9703,
                       role=ROLE_MANAGER, is_owner=True)


async def _set(tenant, slug):
    async with client_for(tenant) as c:
        return await c.put(_slug_url(tenant), json={"slug": slug})


# ---------------------------------------------------------------------------
# Setting the address
# ---------------------------------------------------------------------------

async def test_owner_sets_an_address_and_it_is_normalized(
    db: Session, _override_db, acme
):
    resp = await _set(acme, "  Acme-Co ")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"org_id": acme.org_id, "slug": "acme-co"}

    db.expire_all()
    assert db.query(Organization).get(acme.org_id).slug == "acme-co"


@pytest.mark.parametrize("bad", [
    "ab",                 # too short
    "-acme",              # leading hyphen
    "acme-",              # trailing hyphen
    "acme co",            # space
    "acme!",              # punctuation
    "a" * 64,             # too long
    "12345",              # reads as an id
])
async def test_invalid_addresses_are_refused_with_a_reason(
    db: Session, _override_db, acme, bad
):
    resp = await _set(acme, bad)
    assert resp.status_code == 422, resp.text
    # A plain string, not the generic validation envelope: the UI shows
    # `detail` verbatim, and the owner needs to know which rule they broke.
    assert isinstance(resp.json()["detail"], str)


async def test_reserved_addresses_are_refused(db: Session, _override_db, acme):
    resp = await _set(acme, "login")
    assert resp.status_code == 422
    assert "reserved" in resp.json()["detail"]


async def test_an_address_held_by_another_org_is_refused(
    db: Session, _override_db, acme, stranger
):
    assert (await _set(stranger, "shared-name")).status_code == 200
    resp = await _set(acme, "shared-name")
    assert resp.status_code == 409, resp.text
    assert "taken" in resp.json()["detail"]


async def test_the_address_can_be_cleared(db: Session, _override_db, acme):
    assert (await _set(acme, "acme-co")).status_code == 200
    for empty in (None, "", "   "):
        resp = await _set(acme, empty)
        assert resp.status_code == 200, resp.text
        assert resp.json()["slug"] is None


async def test_a_member_who_is_not_the_owner_cannot_set_it(
    db: Session, _override_db, acme, colleague
):
    """404, matching rename: the owner console should not confirm it exists."""
    resp = await _set(colleague, "acme-co")
    assert resp.status_code == 404


async def test_a_non_member_cannot_set_it(
    db: Session, _override_db, acme, stranger
):
    async with client_for(stranger) as c:
        resp = await c.put(_slug_url(acme), json={"slug": "hijack"})
    assert resp.status_code == 403
    db.expire_all()
    assert db.query(Organization).get(acme.org_id).slug is None


async def test_setting_the_address_is_audited(db: Session, _override_db, acme):
    await _set(acme, "acme-co")

    ev = (db.query(AccessGrantEvent)
            .filter(AccessGrantEvent.event_type == audit.ORG_SLUG_CHANGE,
                    AccessGrantEvent.org_id == acme.org_id).one())
    assert "acme-co" in ev.detail
    assert ev.actor_account_id == acme.account_id


async def test_the_overview_reports_the_address(db: Session, _override_db, acme):
    await _set(acme, "acme-co")
    async with client_for(acme) as c:
        resp = await c.get(f"/api/organizations/{acme.org_id}/overview")
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "acme-co"


# ---------------------------------------------------------------------------
# The public lookup
# ---------------------------------------------------------------------------

async def test_public_lookup_discloses_the_name_and_nothing_else(
    db: Session, _override_db, client, acme
):
    """Pinned as an EXACT key set: this answers for people who have not
    signed in, so a field added here is a disclosure decision."""
    await _set(acme, "acme-co")

    resp = await client.get(BY_SLUG.format(slug="acme-co"))
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"id": acme.org_id, "name": acme.org.name,
                           "slug": "acme-co"}

    # Addresses are case-insensitive on the way in.
    assert (await client.get(BY_SLUG.format(slug="ACME-CO"))).status_code == 200


async def test_public_lookup_404s_for_an_unknown_address(
    db: Session, _override_db, client
):
    resp = await client.get(BY_SLUG.format(slug="nobody-here"))
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Signing in through an org's page
# ---------------------------------------------------------------------------

def _seed_otp(db: Session, account: Account, code: str = "12345678") -> str:
    account.login_otp = _hash_otp(code)
    account.login_otp_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.commit()
    return code


async def _verify(client, account: Account, code: str, **extra):
    return await client.post(VERIFY, json={"email": account.email,
                                           "code": code, **extra})


def _issued_session(resp) -> bool:
    """Did the response carry a jwt cookie? Read from the header: the cookie
    names COOKIE_DOMAIN, which is not the test client's host, so httpx's jar
    drops it before `resp.cookies` is populated."""
    return "jwt=" in resp.headers.get("set-cookie", "")


@pytest.fixture()
def two_org_member(db: Session, acme, stranger):
    """`colleague`, who is also in the stranger's org and lands there today."""
    member = make_tenant(db, slug="slug-acme", account_id=9704,
                         role=ROLE_COMMUNITY_MEMBER, is_owner=False)
    db.add(OrganizationMember(org_id=stranger.org_id,
                              account_id=member.account_id,
                              role=ROLE_COMMUNITY_MEMBER, granted_by="grant"))
    member.account.default_org_id = stranger.org_id
    db.commit()
    return member


async def test_a_member_lands_in_the_requested_org_and_it_becomes_their_default(
    db: Session, _override_db, client, acme, stranger, two_org_member
):
    await _set(acme, "acme-co")
    code = _seed_otp(db, two_org_member.account)

    resp = await _verify(client, two_org_member.account, code, org_slug="acme-co")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["landing_org_id"] == acme.org_id
    assert body["requested_org"] == {"slug": "acme-co", "name": acme.org.name,
                                     "is_member": True}
    assert _issued_session(resp)

    db.expire_all()
    assert (db.query(Account).get(two_org_member.account_id).default_org_id
            == acme.org_id), "the org they signed into is where they land next"


async def test_a_non_member_is_told_so_and_is_not_joined(
    db: Session, _override_db, client, acme, stranger
):
    await _set(acme, "acme-co")
    code = _seed_otp(db, stranger.account)

    resp = await _verify(client, stranger.account, code, org_slug="acme-co")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["landing_org_id"] is None
    assert body["requested_org"] == {"slug": "acme-co", "name": acme.org.name,
                                     "is_member": False}
    assert _issued_session(resp), "the sign-in itself still succeeds"

    db.expire_all()
    assert not db.query(OrganizationMember).filter_by(
        org_id=acme.org_id, account_id=stranger.account_id).first(), \
        "signing in through an org's page must never join anyone to it"
    assert (db.query(Account).get(stranger.account_id).default_org_id
            == stranger.org_id)


async def test_an_unknown_address_is_ignored(db: Session, _override_db, client, acme):
    code = _seed_otp(db, acme.account)
    resp = await _verify(client, acme.account, code, org_slug="no-such-org")
    assert resp.status_code == 200, resp.text
    assert resp.json()["requested_org"] is None
    assert resp.json()["landing_org_id"] is None
    assert _issued_session(resp)


async def test_without_an_address_the_body_is_the_plain_shape(
    db: Session, _override_db, client, acme
):
    """The plain /login flow: a superset of the empty body it used to be."""
    code = _seed_otp(db, acme.account)
    resp = await _verify(client, acme.account, code)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "landing_org_id": None,
                           "requested_org": None}


async def test_a_fresh_signup_through_an_org_page_still_gets_a_personal_workspace(
    db: Session, _override_db, client, acme
):
    """The org page changes where a NEW account lands, not what it owns."""
    await _set(acme, "acme-co")
    newbie = Account(id=9705, email="newbie-9705@x.com")
    db.add(newbie)
    db.commit()
    code = _seed_otp(db, newbie)

    resp = await _verify(client, newbie, code, org_slug="acme-co")
    assert resp.status_code == 200, resp.text
    assert resp.json()["requested_org"]["is_member"] is False

    db.expire_all()
    own = own_org_for(db, newbie.id)
    assert own is not None, "a self-serve signup still gets its workspace"
    assert db.query(Account).get(newbie.id).default_org_id == own.id
    assert not db.query(OrganizationMember).filter_by(
        org_id=acme.org_id, account_id=newbie.id).first()
