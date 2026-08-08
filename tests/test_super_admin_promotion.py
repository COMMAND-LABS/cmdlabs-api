"""
How super admin is given and taken away, and why each is now one thing.

Granting used to take a pair of changes that had to happen together: set the
flag, AND place the account in the platform org. The second was needed because
entitlement resolved from whichever org the caller was acting in, so a promoted
account sitting in its own workspace could browse every org on the platform and
could not open Contacts — which read as a permissions bug and was really a
missing membership row.

Super admins now bypass the module ceiling outright, so they work from
wherever they already are. The placement is gone, and with it the last reason
the platform org had to be a special row.

THAT IS ALSO WHAT MAKES REVOCATION COMPLETE. One boolean going up is the whole
of the grant, so one boolean coming down is the whole of the revoke: no
membership to remove, no ceiling to recompute, nothing left behind that would
still open a door. The tests below pin both directions against the same
account, because a revoke that leaves anything standing is the failure that
would never show up in the grant path.

Deliberately a script rather than an endpoint: no API path writes this in
either direction, so a compromised account cannot escalate itself and there is
no "make super admin" button to click by accident.
"""
from sqlalchemy.orm import Session

from scripts.super_admin import grant, revoke
from src.config import plans_registry as plans
from src.config.modules_registry import MODULE_KEYS
from src.db.models import Account, Organization
from src.deps import OrgContext
from src.services import modules
from src.services.organizations import ensure_membership


def _ctx(account_id, org, is_super_admin=True):
    return OrgContext(
        account_id=account_id, org_id=org.id, role="manager",
        is_owner=True, is_super_admin=is_super_admin)


def _workspace(db, account_id):
    return (db.query(Organization)
              .filter(Organization.owner_account_id == account_id).one())


def test_granting_super_admin_is_enough_on_its_own(db: Session):
    """The whole point: one boolean, and they can work — from their own org.

    No membership anywhere else is required. If this ever needs a second step
    again, the ceiling has quietly started applying to super admins.
    """
    acct = Account(id=8801, email="newsuperadmin@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)
    workspace = _workspace(db, acct.id)

    # Before: an ordinary free plan, in their own workspace.
    assert modules.effective_modules(
        db, _ctx(acct.id, workspace, is_super_admin=False)) != list(MODULE_KEYS)

    grant(dry_run=False, emails=["NewSuperAdmin@cmdlabs.io"], db=db)

    assert acct.is_super_admin is True
    # After: every module, in the SAME org. Nothing joined, nothing moved.
    assert modules.effective_modules(
        db, _ctx(acct.id, workspace)) == list(MODULE_KEYS)


def test_super_admin_are_not_capped_by_the_org_they_are_acting_in(db: Session):
    """Including a tenant's org, which is the case that used to need root.

    Modules decide which SCREENS open; org_id decides which ROWS are visible.
    Super admins opening a screen for an org that never bought that module
    find that org's own empty rows, which is why this bypass is not a data
    question.
    """
    acct = Account(id=8806, email="wideranging@cmdlabs.io", is_super_admin=True)
    db.add(acct)
    db.flush()

    narrow = Organization(name="Narrow",
                          pinned_plan="free")
    db.add(narrow)
    db.flush()

    assert modules.ceiling_for(db, narrow.id) == plans.modules_for_plan(
        plans.PLAN_FREE), "the org is on the narrow plan"
    assert modules.effective_modules(db, _ctx(acct.id, narrow)) == list(
        MODULE_KEYS), "the super admin is not"


def test_a_non_super_admin_owner_is_still_capped_by_their_ceiling(db: Session):
    """The bypass is for SUPER ADMINS. An owner still gets their ceiling."""
    acct = Account(id=8807, email="ordinary@x.test")
    db.add(acct)
    db.flush()

    org = Organization(name="Ordinary",
                       pinned_plan="premium")
    db.add(org)
    db.flush()

    assert modules.effective_modules(
        db, _ctx(acct.id, org, is_super_admin=False)) == plans.modules_for_plan(
            plans.PLAN_PREMIUM), "an owner gets their plan, and no more"


def test_promotion_leaves_their_own_workspace_alone(db: Session):
    """Becoming a super admin must not move them out of the workspace they had.
    """
    acct = Account(id=8802, email="alsosuperadmin@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)
    workspace_id = _workspace(db, acct.id).id

    grant(dry_run=False, emails=["alsosuperadmin@cmdlabs.io"], db=db)

    assert _workspace(db, acct.id).id == workspace_id
    assert acct.default_org_id == workspace_id, "they stay where they were"


def test_promotion_is_idempotent(db: Session):
    acct = Account(id=8803, email="repeatsuperadmin@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)

    assert grant(dry_run=False, emails=["repeatsuperadmin@cmdlabs.io"], db=db) == 1
    assert grant(dry_run=False, emails=["repeatsuperadmin@cmdlabs.io"], db=db) == 0
    assert acct.is_super_admin is True


def test_a_dry_run_writes_nothing(db: Session):
    acct = Account(id=8804, email="maybesuperadmin@cmdlabs.io")
    db.add(acct)
    db.flush()

    grant(dry_run=True, emails=["maybesuperadmin@cmdlabs.io"], db=db)
    assert acct.is_super_admin is False


def test_billing_cannot_touch_super_admin(db: Session):
    """A lapsed card must never remove somebody's platform access.

    This used to need a rule — role_for_subscription() passed admins through
    untouched. It now needs none: super admin lives in its own column and
    billing writes subscription_status, so there is no shared field to get
    wrong.
    """
    acct = Account(id=8805, email="paidsuperadmin@cmdlabs.io", is_super_admin=True,
                   subscription_status="canceled")
    db.add(acct)
    db.flush()

    grant(dry_run=False, emails=[], db=db)
    assert acct.is_super_admin is True
    assert plans.plan_for_account(acct) == plans.PLAN_FREE, (
        "super admins are not billed; their plan is whatever Stripe says")


# ---------------------------------------------------------------------------
# revoking — the direction that has to be right in a hurry
# ---------------------------------------------------------------------------

def test_revoking_closes_the_bypass_on_the_next_request(db: Session):
    """The whole revoke: one boolean, and the extra modules are gone.

    Read through effective_modules rather than the column, because that is
    what a request actually asks. Nothing is cached and nothing is cascaded,
    so there is no window in which the flag says one thing and the menu
    another.
    """
    acct = Account(id=8810, email="departing@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)
    workspace = _workspace(db, acct.id)

    grant(dry_run=False, emails=["departing@cmdlabs.io"], db=db)
    assert modules.effective_modules(
        db, _ctx(acct.id, workspace)) == list(MODULE_KEYS)

    assert revoke(dry_run=False, emails=["departing@cmdlabs.io"], db=db) == 1

    assert acct.is_super_admin is False
    assert modules.effective_modules(
        db, _ctx(acct.id, workspace, is_super_admin=False)) != list(MODULE_KEYS)


def test_revoking_leaves_their_own_workspace_untouched(db: Session):
    """They stop being staff; they do not stop being a customer.

    This is what "the flag is the whole of it" has to mean in practice — the
    org they own, their membership in it and the plan they pay for are none of
    super admin's business, and revoking must not disturb any of them.
    """
    acct = Account(id=8811, email="stillacustomer@cmdlabs.io",
                   subscription_status="active")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)
    workspace_id = _workspace(db, acct.id).id

    grant(dry_run=False, emails=["stillacustomer@cmdlabs.io"], db=db)
    revoke(dry_run=False, emails=["stillacustomer@cmdlabs.io"], db=db)

    assert _workspace(db, acct.id).id == workspace_id
    assert acct.default_org_id == workspace_id
    assert plans.plan_for_account(acct) == plans.PLAN_PREMIUM, (
        "their subscription is untouched by losing the flag")


def test_revoking_is_idempotent(db: Session):
    """Twice is not an error, and neither is somebody who never had it.

    Both return 0 because the count is of accounts CHANGED. An operator
    running this under pressure needs "nothing happened" to be distinguishable
    from "it worked", in both directions.
    """
    acct = Account(id=8812, email="neverhadit@cmdlabs.io")
    db.add(acct)
    db.flush()

    assert revoke(dry_run=False, emails=["neverhadit@cmdlabs.io"], db=db) == 0
    assert acct.is_super_admin is False

    grant(dry_run=False, emails=["neverhadit@cmdlabs.io"], db=db)
    assert revoke(dry_run=False, emails=["neverhadit@cmdlabs.io"], db=db) == 1
    assert revoke(dry_run=False, emails=["neverhadit@cmdlabs.io"], db=db) == 0


def test_a_dry_run_revoke_writes_nothing(db: Session):
    """The dangerous direction gets the same default as the safe one."""
    acct = Account(id=8813, email="notyet@cmdlabs.io", is_super_admin=True)
    db.add(acct)
    db.flush()

    revoke(dry_run=True, emails=["notyet@cmdlabs.io"], db=db)
    assert acct.is_super_admin is True


def test_revoking_only_touches_the_named_account(db: Session):
    """A revoke must never widen past its --email list.

    Cheap to assert and worth pinning: the failure mode is removing everyone's
    access at once, which is exactly the kind of thing you do not discover
    until somebody tries to open the admin surface.
    """
    keep = Account(id=8814, email="keepit@cmdlabs.io", is_super_admin=True)
    lose = Account(id=8815, email="loseit@cmdlabs.io", is_super_admin=True)
    db.add_all([keep, lose])
    db.flush()

    revoke(dry_run=False, emails=["loseit@cmdlabs.io"], db=db)

    assert lose.is_super_admin is False
    assert keep.is_super_admin is True
