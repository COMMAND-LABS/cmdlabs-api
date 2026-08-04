"""
Organization membership resolution.

An organization is the tenant. The root org (slug 'root') is org #1 and is NOT
a special case — it is the first instance of the same object every customer
gets. Signup lands an account in whichever org's page it came through; today
that is always root.

CANONICAL FILE. Keep in sync with cmdlabs-agent-api via ./sync-schemas.sh if
that service ever needs to create memberships (it currently only reads them).
"""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Account, Organization, OrganizationMember
from src.services import audit

logger = logging.getLogger(__name__)

ROOT_SLUG = "root"

# Tiers seeded on the root org by migration e8f9a0b1c2d3.
TIER_FREE = "free"
TIER_PREMIUM = "premium"
TIER_ORG_OWNER = "org_owner"

# Entitlement provenance. 'subscription' is owned by the Stripe webhook and
# lapses with billing; 'grant' is set by an owner and is NEVER written by any
# webhook — that asymmetry is what lets staff comp a client into a paid tier.
GRANTED_BY_SUBSCRIPTION = "subscription"
GRANTED_BY_GRANT = "grant"

ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")


def get_org_by_slug(db: Session, slug: str) -> Organization | None:
    return db.query(Organization).filter(Organization.slug == slug).first()


def get_root_org(db: Session) -> Organization | None:
    return get_org_by_slug(db, ROOT_SLUG)


def default_tier_for(account: Account) -> tuple[str, str]:
    """(tier_key, granted_by) a brand-new membership should start on.

    Derived from the subscription rather than accounts.role, for the same
    reason the backfill migration does it that way: a row whose role has
    drifted out of agreement with Stripe would otherwise be handed a paid tier
    marked 'grant', and grants are deliberately immune to webhooks — the lapse
    would never take effect.
    """
    if account.role == "admin":
        # Platform staff. is_owner is set separately by the caller.
        return TIER_ORG_OWNER, GRANTED_BY_GRANT
    if account.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return TIER_PREMIUM, GRANTED_BY_SUBSCRIPTION
    return TIER_FREE, GRANTED_BY_GRANT


def ensure_membership(
    db: Session,
    account: Account,
    org: Organization | None = None,
) -> OrganizationMember | None:
    """Ensure `account` is a member of `org` (default: root). Idempotent.

    Called at first verified login rather than at account creation, because
    accounts are INSERTed in /request-code — before the OTP is checked. An
    unverified squatter therefore gets an account row and no membership, and
    since they can never obtain a JWT they never need one.

    Returns None (without raising) when the root org does not exist yet, i.e.
    the org migration has not been applied. Login must keep working in that
    window; nothing reads memberships until the org context ships.
    """
    if org is None:
        org = get_root_org(db)
        if org is None:
            logger.warning(
                "[ORG] No root org — skipping membership for account %s. "
                "Has migration e8f9a0b1c2d3 been applied?", account.id,
            )
            return None

    existing = (
        db.query(OrganizationMember)
        .filter(
            OrganizationMember.org_id == org.id,
            OrganizationMember.account_id == account.id,
        )
        .first()
    )
    if existing:
        if account.default_org_id is None:
            account.default_org_id = org.id
            db.commit()
        return existing

    tier_key, granted_by = default_tier_for(account)
    member = OrganizationMember(
        org_id=org.id,
        account_id=account.id,
        tier_key=tier_key,
        granted_by=granted_by,
        is_owner=(account.role == "admin" and org.slug == ROOT_SLUG),
    )
    db.add(member)
    if account.default_org_id is None:
        account.default_org_id = org.id

    # Joining an org is one of the most consequential events on the platform —
    # the moment someone gains access to a tenant's data — and it was
    # previously unlogged.
    audit.record_membership(
        db, event_type=audit.MEMBER_ADD, org_id=org.id,
        account_id=account.id, tier_key=tier_key,
        actor_account_id=account.id,
    )

    try:
        db.commit()
    except IntegrityError:
        # Two concurrent /verify-code calls for the same account raced on
        # uq_org_member. Whichever lost re-reads the winner's row rather than
        # failing the login.
        db.rollback()
        return (
            db.query(OrganizationMember)
            .filter(
                OrganizationMember.org_id == org.id,
                OrganizationMember.account_id == account.id,
            )
            .first()
        )

    db.refresh(member)
    return member
