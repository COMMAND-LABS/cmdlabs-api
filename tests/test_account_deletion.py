"""
DELETE /api/accounts/me: cancel billing, keep the team's data, never orphan a team.

See services/account_deletion for what the bare db.delete() used to get wrong.
"""
from unittest.mock import patch

import stripe
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import roles_registry as roles
from src.db.models import (
    Account,
    Contact,
    Logins,
    Organization,
    OrganizationMember,
    UsageCredits,
)
from src.services.organizations import ensure_membership, own_org_for
from tests.conftest import make_token

CANCEL = "src.routers.accounts.delete.cancel_subscription_now"


def _signup(db: Session, account_id: int, **kw) -> Account:
    """An account in the state a real signup leaves it: credits, a login, a workspace."""
    acct = Account(id=account_id, email=f"d{account_id}@delete.test", **kw)
    db.add(acct)
    db.flush()
    db.add(UsageCredits(account_id=acct.id, amount=0))
    db.add(Logins(account_id=acct.id, ip_address="127.0.0.1"))
    ensure_membership(db, acct)
    db.flush()
    return acct


async def _delete(client: AsyncClient, acct: Account):
    token = make_token(email=acct.email, user_id=acct.id)
    return await client.delete("/api/accounts/me",
                               headers={"Authorization": f"Bearer {token}"})


async def test_a_signup_can_delete_itself_and_its_workspace(client, db: Session):
    acct = _signup(db, 9301)
    org_id = own_org_for(db, acct.id).id

    with patch(CANCEL) as cancel:
        resp = await _delete(client, acct)

    assert resp.status_code == 204, resp.text
    cancel.assert_not_called()
    assert db.query(Account).filter(Account.id == 9301).first() is None
    assert db.query(Organization).filter(Organization.id == org_id).first() is None


async def test_deleting_cancels_the_subscription(client, db: Session):
    acct = _signup(db, 9302, stripe_subscription_id="sub_live",
                   subscription_status="active")

    with patch(CANCEL) as cancel:
        resp = await _delete(client, acct)

    assert resp.status_code == 204, resp.text
    cancel.assert_called_once_with("sub_live")


async def test_a_stripe_failure_keeps_the_account(client, db: Session):
    acct = _signup(db, 9303, stripe_subscription_id="sub_live",
                   subscription_status="past_due")

    with patch(CANCEL, side_effect=stripe.error.APIConnectionError("down")):
        resp = await _delete(client, acct)

    assert resp.status_code == 502
    assert db.query(Account).filter(Account.id == 9303).first() is not None


async def test_the_owner_of_a_team_must_hand_it_over_first(client, db: Session):
    owner = _signup(db, 9304)
    colleague = _signup(db, 9305)
    team = own_org_for(db, owner.id)
    db.add(OrganizationMember(org_id=team.id, account_id=colleague.id,
                              role=roles.ROLE_MANAGER, granted_by="grant"))
    db.flush()

    with patch(CANCEL):
        resp = await _delete(client, owner)

    assert resp.status_code == 409
    assert team.name in resp.json()["detail"]
    assert db.query(Account).filter(Account.id == 9304).first() is not None


async def test_a_colleagues_contacts_stay_with_the_team(client, db: Session):
    owner = _signup(db, 9306)
    colleague = _signup(db, 9307)
    team = own_org_for(db, owner.id)
    db.add(OrganizationMember(org_id=team.id, account_id=colleague.id,
                              role=roles.ROLE_MANAGER, granted_by="grant"))
    contact = Contact(org_id=team.id, account_id=colleague.id,
                      first_name="Kept", email="kept@delete.test")
    db.add(contact)
    db.flush()
    contact_id = contact.id

    with patch(CANCEL):
        resp = await _delete(client, colleague)

    assert resp.status_code == 204, resp.text
    db.expire_all()
    kept = db.query(Contact).filter(Contact.id == contact_id).first()
    assert kept is not None, "the team's contact was deleted with its author"
    assert kept.account_id == owner.id
