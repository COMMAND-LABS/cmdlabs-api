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
        granted_modules=[],
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
    assert ctx.tier_key == "free"


async def test_cookie_selects_a_joined_org(db, test_account, test_org, other_org):
    db.add(OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        tier_key="premium", granted_by="grant", is_owner=True,
    ))
    db.flush()

    ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert ctx.org_id == other_org.id
    assert ctx.tier_key == "premium"
    assert ctx.is_owner is True
    # Org-level facts travel with the org, not the account.
    assert ctx.org_id == other_org.id


# ---------------------------------------------------------------------------
# the cookie is not authority
# ---------------------------------------------------------------------------

async def test_cookie_naming_a_foreign_org_is_refused(db, test_account, other_org):
    """The caller is NOT a member of other_org — this must 403, not fall back.

    Falling back to the default org would turn a tampered or stale cookie into
    "you are quietly somewhere else" rather than a visible error, and would
    hide exactly the case worth seeing.
    """
    with pytest.raises(HTTPException) as exc:
        await _resolve(db, test_account.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert exc.value.status_code == 403


async def test_revoked_membership_is_refused_on_the_very_next_request(
    db, test_account, test_org, other_org
):
    """No token re-issue, no cache to expire — this is why org lives in a
    cookie rather than in the 7-day JWT."""
    member = OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        tier_key="premium", granted_by="grant", is_owner=False,
    )
    db.add(member)
    db.flush()

    cookies = {ORG_COOKIE_NAME: str(other_org.id)}
    assert (await _resolve(db, test_account.id, cookies)).org_id == other_org.id

    db.delete(member)
    db.flush()

    with pytest.raises(HTTPException) as exc:
        await _resolve(db, test_account.id, cookies)
    assert exc.value.status_code == 403


async def test_garbage_cookie_falls_back_to_default(db, test_account, test_org):
    for junk in ("", "abc", "../../etc/passwd", "1; DROP TABLE"):
        ctx = await _resolve(db, test_account.id, {ORG_COOKIE_NAME: junk})
        assert ctx.org_id == test_org.id


async def test_api_key_path_ignores_the_cookie(db, test_account, test_org, other_org):
    """An API key carries no org of its own, so honouring a cookie beside it
    would let a key issued for one org be aimed at another."""
    db.add(OrganizationMember(
        org_id=other_org.id, account_id=test_account.id,
        tier_key="premium", granted_by="grant", is_owner=False,
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
    """Staff bypass MODULES, never org_id.

    Reading another org's data requires joining it, which leaves a membership
    row that org can see. An invisible bypass would make the audit log a lie.
    """
    staff = Account(id=500, email="staff@cmdlabs.io", is_staff=True,
                    default_org_id=test_org.id)
    db.add(staff)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id, account_id=staff.id,
        tier_key="org_owner", granted_by="grant", is_owner=True,
    ))
    db.flush()

    ctx = await _resolve(db, staff.id)
    assert ctx.is_super_admin is True

    with pytest.raises(HTTPException) as exc:
        await _resolve(db, staff.id, {ORG_COOKIE_NAME: str(other_org.id)})
    assert exc.value.status_code == 403
