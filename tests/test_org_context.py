"""
Org context resolution — the single chokepoint that decides which tenant a
request acts in.

The property under test throughout: the active-org cookie is DATA, never
authority. It names an org; membership in that org is re-checked against the
database on every request. That is what makes removing someone take effect
immediately, with no token to re-issue.
"""
import pytest

from src.db.models import Account, Organization, OrganizationMember
from src.deps import ORG_COOKIE_NAME, get_org_context
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Minimal stand-in — get_org_context only ever reads request.cookies."""

    def __init__(self, cookies: dict | None = None):
        self.cookies = cookies or {}


def _claims(account_id: int, auth_type: str = "jwt") -> dict:
    return {"email": f"a{account_id}@x.com", "id": account_id, "auth_type": auth_type}


async def _resolve(db, account_id, cookies=None, auth_type="jwt"):
    return await get_org_context(
        _FakeRequest(cookies), db=db, auth=_claims(account_id, auth_type)
    )


@pytest.fixture()
def other_org(db) -> Organization:
    org = Organization(
        name="Acme",
        pinned_plan="premium",
    )
    db.add(org)
    db.flush()
    return org


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

async def test_falls_back_to_default_org_without_cookie(db, test_account, test_org):
    ctx = await _resolve(db, test_account.id)
    assert ctx.org_id == test_org.id
    assert ctx.role == "manager"


async def test_cookie_selects_a_joined_org(db, test_account, test_org, other_org):
    db.add(OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        role="manager", granted_by="grant",
    ))
    # Ownership is the ORG's column, so making this account the owner is now
    # said here rather than on the membership row. The membership is what lets
    # them IN; this is what makes them the owner once inside.
    other_org.owner_account_id = test_account.id
    db.flush()

    ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert ctx.org_id == other_org.id
    assert ctx.role == "manager"
    assert ctx.is_owner is True
    # Org-level facts travel with the org, not the account.
    assert ctx.org_id == other_org.id


# ---------------------------------------------------------------------------
# the cookie is not authority
# ---------------------------------------------------------------------------

async def test_cookie_naming_a_foreign_org_is_ignored(
    db, test_account, test_org, other_org
):
    """The caller is NOT a member of other_org, so the cookie buys nothing.

    THIS USED TO ASSERT 403, and the reasoning here was that falling back would
    turn a tampered or stale cookie into "you are quietly somewhere else"
    rather than a visible error. Both halves came apart in practice.

    The cookie is written by client JS with a one-year max-age and was cleared
    by nothing — not sign-out, not sign-in — so it outlived the session AND the
    membership. Two ordinary things then bricked an account: an owner removing
    somebody, and a second account signing in on a browser where anybody had
    ever used the switcher.

    And "visible error" overstated it. EVERY org-scoped route 403'd, including
    /organizations/mine, so the switcher that would have fixed it could not
    render. The product became a wall of error toasts with no way out short of
    devtools, which is not feedback.

    WHAT IS UNCHANGED IS THE PART THAT MATTERS, and it is what this asserts:
    the caller does not end up in other_org. The cookie is still not authority.
    The fallback target is the account's own default_org_id — a value only the
    server writes — so a tampered cookie is ignored rather than obeyed.
    """
    ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert ctx.org_id == test_org.id
    assert ctx.org_id != other_org.id, "never the org they named and are not in"


async def test_a_foreign_cookie_with_no_default_to_fall_back_to_is_refused(
    db, other_org
):
    """Fail closed is still the floor.

    The fallback needs somewhere to fall back TO. An account with no default
    org and a cookie naming one it is not in gets the refusal, because there is
    no honest answer to give it.
    """
    orphan = Account(id=998, email="orphan-cookie@x.com")
    db.add(orphan)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        await _resolve(db, orphan.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert exc.value.status_code == 403


async def test_revoked_membership_bites_on_the_very_next_request(
    db, test_account, test_org, other_org
):
    """No token re-issue, no cache to expire — this is why org lives in a
    cookie rather than in the 7-day JWT.

    The removal is just as immediate as when this raised: the very next request
    stops resolving to other_org. What changed is where they land instead —
    their own workspace rather than an error they cannot act on.
    """
    member = OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        role="manager", granted_by="grant",
    )
    db.add(member)
    db.flush()

    cookies = {ORG_COOKIE_NAME: str(other_org.id)}
    assert (await _resolve(db, test_account.id, cookies)).org_id == other_org.id

    db.delete(member)
    db.flush()

    assert (await _resolve(db, test_account.id, cookies)).org_id == test_org.id


async def test_garbage_cookie_falls_back_to_default(db, test_account, test_org):
    for junk in ("", "abc", "../../etc/passwd", "1; DROP TABLE"):
        ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: junk})
        assert ctx.org_id == test_org.id


async def test_api_key_path_ignores_the_cookie(db, test_account, test_org, other_org):
    """An API key carries no org of its own, so honouring a cookie beside it
    would let a key issued for one org be aimed at another."""
    db.add(OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        role="manager", granted_by="grant",
    ))
    db.flush()

    ctx = await _resolve(
        db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)}, auth_type="api_key"
    )
    assert ctx.org_id == test_org.id, "API-key path must ignore the org cookie"


# ---------------------------------------------------------------------------
# fail closed
# ---------------------------------------------------------------------------

async def test_account_with_no_membership_is_refused(db, test_org):
    orphan = Account(id=999, email="orphan@x.com")
    db.add(orphan)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        await _resolve(db, orphan.id)
    assert exc.value.status_code == 403


async def test_super_admin_does_not_bypass_org_membership(db, test_org, other_org):
    """Super admins bypass MODULES, never org_id.

    Reading another org's data requires joining it, which leaves a membership
    row that org can see. An invisible bypass would make the audit log a lie.
    """
    super_admin = Account(id=500, email="superadmin@cmdlabs.io", is_super_admin=True,
                    default_org_id=test_org.id)
    db.add(super_admin)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id, account_id=super_admin.id,
        role="manager", granted_by="grant",
    ))
    db.flush()

    ctx = await _resolve(db, super_admin.id)
    assert ctx.is_super_admin is True

    # Pointing the cookie at an org they have not joined does not get them in.
    # It resolves to their own org instead of raising — see
    # test_cookie_naming_a_foreign_org_is_ignored — and the assertion that
    # carries this test is the same one it always made: not other_org.
    elsewhere = await _resolve(db, super_admin.id,
                               {ORG_COOKIE_NAME: str(other_org.id)})
    assert elsewhere.org_id == test_org.id
    assert elsewhere.org_id != other_org.id, (
        "a super admin reads another org's data by JOINING it, which leaves a "
        "membership row that org can see")


# ---------------------------------------------------------------------------
# ownership has one home
# ---------------------------------------------------------------------------

async def test_ownership_is_read_from_the_org_not_the_membership(
    db, test_account, other_org,
):
    """The point of collapsing it: there is nothing left to disagree with.

    Ownership was stored twice — organizations.owner_account_id and a per-row
    is_owner flag — and drifted, leaving accounts that owned an org they could
    not open. Moving the owner column is now the ONLY way to change who owns an
    org, and the context follows it on the next request with nothing to keep in
    step.
    """
    db.add(OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        role="manager", granted_by="grant",
    ))
    other_org.owner_account_id = None
    db.flush()

    ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert ctx.is_owner is False, "a member of an ownerless org is not its owner"

    other_org.owner_account_id = test_account.id
    db.flush()
    ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert ctx.is_owner is True, "and the very next request sees it"


async def test_the_membership_row_can_no_longer_claim_ownership(db):
    """The column is gone, so the second opinion cannot be written at all.

    Asserted against the mapper rather than by trying to set it, because the
    guarantee is that there is no such field to set — in this service or in the
    agent runtime that mirrors this model byte for byte.
    """
    assert not hasattr(OrganizationMember, "is_owner"), (
        "is_owner is organizations.owner_account_id now, and nowhere else")
