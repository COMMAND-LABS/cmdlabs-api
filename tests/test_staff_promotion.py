"""
How a platform admin is established.

Two things have to happen together, and only one of them is obvious. Granting
role='admin' unlocks the platform-admin surface — but entitlement is resolved
from whichever org the caller is ACTING in, and a promoted account is acting in
its own personal workspace, whose ceiling is the free one. Without a membership
in the platform org, a brand-new platform admin can browse every org on the
platform and cannot open Contacts. That reads as a permissions bug and is
really a missing row.

Deliberately a script rather than an endpoint: no API path grants admin, so a
compromised account cannot escalate itself and there is no "make admin" button
to click by accident.
"""
from sqlalchemy.orm import Session

from scripts.sync_account_roles import sync
from src.config import plans_registry as plans
from src.db.models import Account, Organization, OrganizationMember
from src.deps import OrgContext
from src.services import modules
from src.services.organizations import PLATFORM_SLUG, ensure_membership


def _ctx(account_id, org, is_super_admin=True):
    return OrgContext(
        account_id=account_id, org_id=org.id, org_slug=org.slug,
        tier_key="owner", is_owner=True, is_super_admin=is_super_admin,
        org_status="active",
    )


def test_promotion_places_staff_in_the_platform_org(db: Session, test_org):
    """The whole point: one command, and they can actually work."""
    acct = Account(id=8801, email="newstaff@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)

    workspace = (db.query(Organization)
                   .filter(Organization.owner_account_id == acct.id,
                           Organization.slug.is_(None)).one())
    # Before: staff-in-name-only would see the free plan and nothing more.
    #
    # Asserted against the registry rather than a literal list. What the free
    # plan contains is a product decision that moves (it gained `courses` when
    # the catalog shipped); that a promoted account is still stuck on it until
    # the membership exists is the invariant this test is about.
    assert (modules.effective_modules(db, _ctx(acct.id, workspace))
            == plans.modules_for_plan(plans.PLAN_FREE))

    sync(dry_run=False, make_admin=["NewStaff@cmdlabs.io"], db=db)

    assert acct.role == "admin"
    membership = (db.query(OrganizationMember)
                    .filter(OrganizationMember.org_id == test_org.id,
                            OrganizationMember.account_id == acct.id).one())
    assert membership.is_owner is True
    assert membership.granted_by == "grant", "staff access is never billed"
    assert acct.default_org_id == test_org.id, "they land where staff work"

    # After: the platform org's full ceiling.
    assert modules.effective_modules(db, _ctx(acct.id, test_org)) == list(
        test_org.granted_modules)


def test_promotion_leaves_their_own_workspace_alone(db: Session, test_org):
    """Becoming staff must not take away the workspace they already had."""
    acct = Account(id=8802, email="alsostaff@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)
    workspace_id = (db.query(Organization.id)
                      .filter(Organization.owner_account_id == acct.id,
                              Organization.slug.is_(None)).scalar())

    sync(dry_run=False, make_admin=["alsostaff@cmdlabs.io"], db=db)

    still_theirs = (db.query(OrganizationMember)
                      .filter(OrganizationMember.org_id == workspace_id,
                              OrganizationMember.account_id == acct.id).one())
    assert still_theirs.is_owner is True


def test_promotion_is_idempotent(db: Session, test_org):
    acct = Account(id=8803, email="repeatstaff@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)

    sync(dry_run=False, make_admin=["repeatstaff@cmdlabs.io"], db=db)
    sync(dry_run=False, make_admin=["repeatstaff@cmdlabs.io"], db=db)

    assert (db.query(OrganizationMember)
              .filter(OrganizationMember.org_id == test_org.id,
                      OrganizationMember.account_id == acct.id).count() == 1)


def test_a_dry_run_writes_nothing(db: Session, test_org):
    acct = Account(id=8804, email="maybestaff@cmdlabs.io")
    db.add(acct)
    db.flush()
    ensure_membership(db, acct)

    sync(dry_run=True, make_admin=["maybestaff@cmdlabs.io"], db=db)

    assert not (db.query(OrganizationMember)
                  .filter(OrganizationMember.org_id == test_org.id,
                          OrganizationMember.account_id == acct.id).first())


def test_billing_never_demotes_staff(db: Session, test_org):
    """Admins pass through role_for_subscription untouched — a lapsed card must
    not remove someone's platform access."""
    acct = Account(id=8805, email="paidstaff@cmdlabs.io", role="admin",
                   subscription_status="canceled")
    db.add(acct)
    db.flush()

    sync(dry_run=False, make_admin=[], db=db)
    assert acct.role == "admin"


def test_the_platform_org_is_found_by_slug_not_id(db: Session, test_org):
    """Nothing may work because root happens to be org #1."""
    assert test_org.slug == PLATFORM_SLUG
