"""
PUT /api/organizations/{org_id}/active — remember where the caller is acting.

Login clears the active-org cookie on purpose, so without this nothing
remembered a switch: default_org_id was written only when NULL and every
sign-in dropped people back into the first org they ever joined. The tests
pin that a switch now sticks across a sign-in (read back through /mine with
no cookie, which is exactly what a fresh session looks like to the API), and
that only a MEMBER can point their default at an org.
"""
import pytest
from sqlalchemy.orm import Session

from src.config.roles_registry import ROLE_COMMUNITY_MEMBER, ROLE_MANAGER
from src.db.models import Account, OrganizationMember
from tests.org_isolation import client_for, make_tenant

MINE = "/api/organizations/mine"


def _active_url(org_id: int) -> str:
    return f"/api/organizations/{org_id}/active"


@pytest.fixture()
def home(db: Session):
    """An account and the org it owns."""
    return make_tenant(db, slug="active-home", account_id=9711,
                       role=ROLE_MANAGER, is_owner=True)


@pytest.fixture()
def team(db: Session, home):
    """A second org the same account is a plain member of."""
    team = make_tenant(db, slug="active-team", account_id=9712,
                       role=ROLE_MANAGER, is_owner=True)
    db.add(OrganizationMember(org_id=team.org_id, account_id=home.account_id,
                              role=ROLE_COMMUNITY_MEMBER, granted_by="grant"))
    db.commit()
    return team


@pytest.fixture()
def elsewhere(db: Session):
    """An org the account has nothing to do with."""
    return make_tenant(db, slug="active-elsewhere", account_id=9713,
                       role=ROLE_MANAGER, is_owner=True)


def _default_of(db: Session, account_id: int):
    db.expire_all()
    return db.query(Account).get(account_id).default_org_id


async def test_switching_sticks_across_a_sign_in(
    db: Session, _override_db, home, team
):
    assert _default_of(db, home.account_id) == home.org_id

    async with client_for(home) as c:
        resp = await c.put(_active_url(team.org_id))
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"active_org_id": team.org_id}

        # No org cookie on this client: this is what the first request of a
        # fresh session looks like, and it must already be in the team.
        mine = await c.get(MINE)
    assert mine.json()["active_org_id"] == team.org_id
    assert _default_of(db, home.account_id) == team.org_id

    # And back again.
    async with client_for(home) as c:
        assert (await c.put(_active_url(home.org_id))).status_code == 200
        mine = await c.get(MINE)
    assert mine.json()["active_org_id"] == home.org_id


async def test_a_non_member_cannot_point_their_default_at_an_org(
    db: Session, _override_db, home, elsewhere
):
    async with client_for(home) as c:
        resp = await c.put(_active_url(elsewhere.org_id))
    assert resp.status_code == 403, resp.text
    assert _default_of(db, home.account_id) == home.org_id


async def test_an_account_with_no_default_yet_can_only_choose_an_org_it_is_in(
    db: Session, _override_db, home
):
    """A pending invitee has NULL default_org_id and no memberships. The path
    gate does not depend on either, so it refuses until they are a member."""
    invitee = Account(id=9714, email="invitee-9714@x.com", default_org_id=None)
    db.add(invitee)
    db.commit()

    from tests.conftest import make_token
    from httpx import ASGITransport, AsyncClient
    from src.main import app
    token = make_token(email=invitee.email, user_id=invitee.id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"Authorization": f"Bearer {token}"}) as c:
        assert (await c.put(_active_url(home.org_id))).status_code == 403

        db.add(OrganizationMember(org_id=home.org_id, account_id=invitee.id,
                                  role=ROLE_COMMUNITY_MEMBER, granted_by="grant"))
        db.commit()
        resp = await c.put(_active_url(home.org_id))
    assert resp.status_code == 200, resp.text
    assert _default_of(db, invitee.id) == home.org_id
