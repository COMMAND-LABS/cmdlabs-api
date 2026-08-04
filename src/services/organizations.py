"""
Organization membership: every account owns one, from its first verified login.

THE RULE, AND IT HAS NO EXCEPTIONS
----------------------------------
An organization is the tenant, and every account has one of its own. A signup
gets a PERSONAL WORKSPACE — an org with a single member who is its owner — and
a team is the very same object with more members in it. Nothing is a special
case, which is what lets the tenancy rule be `org_id == ctx.org_id` and nothing
else (see services/org_scope.tenant_predicate).

It did not start that way. Every account used to land in the root org, and a
`data_scope='personal'` flag stopped those strangers seeing each other's
contacts. That flag existed for exactly one row and was the only reason row
visibility depended on anything besides org_id. Migrations e3f4a5b6c7d8 and
f4a5b6c7d8e9 split the orgs apart and removed it.

Root is now purely the PLATFORM org: staff only, and the home of published
catalog content. That separation is also what makes publishing honest — root
used to be the platform's content org and the public lobby at once, so
"belongs to the platform org" could not tell a staff-authored lesson from a
stranger's private agent.

WHY THE CEILING, NOT THE TIER
-----------------------------
A personal org's member is its owner, and an owner bypasses the tier layer
(services/modules.effective_modules) so that one bad save in the matrix can
never lock them out of the screen that fixes it. The consequence is that for a
personal org the CEILING is the whole entitlement — it is what billing raises
and lowers. Tiers only start meaning anything once an org contains somebody who
is not its owner.

CANONICAL FILE. Keep in sync with cmdlabs-agent-api via ./sync-schemas.sh if
that service ever needs to create memberships (it currently only reads them).
"""
import logging

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.models import Account, Organization, OrganizationMember, OrganizationTier
from src.services import audit

logger = logging.getLogger(__name__)

# The platform's own org. Staff only.
PLATFORM_SLUG = "root"
ROOT_SLUG = PLATFORM_SLUG  # back-compat for existing importers

TIER_OWNER = "owner"
TIER_MEMBER = "member"

# Retained because the root org still carries these and the migration seeded
# them; a personal workspace uses `owner` instead.
TIER_FREE = "free"
TIER_PREMIUM = "premium"
TIER_ORG_OWNER = "org_owner"

# Entitlement provenance. 'subscription' is owned by the Stripe webhook and
# lapses with billing; 'grant' is set by a human and is NEVER written by any
# webhook — that asymmetry is what lets staff comp a client into paid access.
GRANTED_BY_SUBSCRIPTION = "subscription"
GRANTED_BY_GRANT = "grant"

ACTIVE_SUBSCRIPTION_STATUSES = ("active", "trialing")

# Ceilings for a brand-new personal workspace. Kept in step with migration
# e3f4a5b6c7d8, which seeded exactly these for the accounts that already
# existed. Duplicated rather than imported because a migration is a
# point-in-time snapshot and must not follow this file as it changes.
FREE_CEILING = ["home", "membership", "settings"]
PREMIUM_CEILING = ["agents", "agent_chat", "credentials", "membership", "settings"]


def get_org_by_slug(db: Session, slug: str) -> Organization | None:
    return db.query(Organization).filter(Organization.slug == slug).first()


def get_platform_org(db: Session) -> Organization | None:
    return get_org_by_slug(db, PLATFORM_SLUG)


get_root_org = get_platform_org  # back-compat


def ceiling_for_account(account: Account) -> list:
    """The modules a new personal workspace starts with.

    Derived from the subscription rather than accounts.role, for the same
    reason the backfill migrations do it that way: a row whose role has drifted
    out of agreement with Stripe would otherwise be handed paid modules that no
    webhook would ever take back.
    """
    if account.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return list(PREMIUM_CEILING)
    return list(FREE_CEILING)


def personal_org_for(db: Session, account_id: int) -> Organization | None:
    """The workspace this account owns, if it has one."""
    return (
        db.query(Organization)
        .filter(Organization.owner_account_id == account_id,
                Organization.slug.is_(None))
        .first()
    )


def ensure_membership(db: Session, account: Account,
                      org: Organization | None = None) -> OrganizationMember | None:
    """Ensure `account` can act somewhere. Idempotent.

    With no `org`, this creates (or finds) the account's own personal
    workspace. Passing an org joins that one instead — which is what an invite
    will do.

    Called at first VERIFIED login rather than at account creation, because
    accounts are INSERTed in /request-code, before the OTP is checked. An
    unverified squatter therefore gets an account row and no org, and since
    they can never obtain a JWT they never need one — and no empty workspace is
    created for an address nobody controls.
    """
    if org is None:
        org = personal_org_for(db, account.id)
        if org is None:
            org = _create_personal_org(db, account)

    existing = (
        db.query(OrganizationMember)
        .filter(OrganizationMember.org_id == org.id,
                OrganizationMember.account_id == account.id)
        .first()
    )
    if existing:
        if account.default_org_id is None:
            account.default_org_id = org.id
            db.commit()
        return existing

    is_owner = (org.owner_account_id == account.id)
    tier_key = TIER_OWNER if is_owner else TIER_MEMBER
    member = OrganizationMember(
        org_id=org.id,
        account_id=account.id,
        tier_key=tier_key,
        # Never 'subscription': a membership is not what billing acts on. For a
        # personal org billing moves the CEILING, and for a team org the member
        # is there because an owner put them there.
        granted_by=GRANTED_BY_GRANT,
        is_owner=is_owner,
    )
    db.add(member)
    if account.default_org_id is None:
        account.default_org_id = org.id

    # Joining an org is the moment someone gains access to a tenant's data —
    # the single most consequential event on the platform, and it was
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
            .filter(OrganizationMember.org_id == org.id,
                    OrganizationMember.account_id == account.id)
            .first()
        )

    db.refresh(member)
    return member


def _create_personal_org(db: Session, account: Account) -> Organization:
    """A workspace of one, owned by its only member.

    No slug. A personal workspace has no public page, and inventing
    `user-273` would hand the owner a permanent identity they never chose —
    slugs are immutable precisely because they are public. They pick one if and
    when they turn this into a team.
    """
    name = (account.email or "").split("@")[0] or "Workspace"
    org = Organization(
        slug=None,
        name=name,
        owner_account_id=account.id,
        granted_modules=ceiling_for_account(account),
        status="active",
        # Billing owns this org's ceiling until staff overrides it, at which
        # point admin.set_ceiling flips this to 'grant' and the webhook stops
        # touching it. That is the comp mechanism, one level up from
        # OrganizationMember.granted_by.
        ceiling_managed_by=GRANTED_BY_SUBSCRIPTION,
    )
    db.add(org)
    db.flush()

    # Seeded so that converting this workspace into a team is picking a slug
    # and inviting somebody — not first discovering the tiers page is empty.
    # `member` starts with nothing: an invited person gets what the owner
    # deliberately checks in the matrix, never a default they did not choose.
    db.add(OrganizationTier(org_id=org.id, tier_key=TIER_OWNER, label="Owner",
                            modules=list(org.granted_modules)))
    db.add(OrganizationTier(org_id=org.id, tier_key=TIER_MEMBER, label="Member",
                            modules=[]))
    db.flush()

    audit.record_org_change(
        db, event_type=audit.ORG_CREATE, org_id=org.id,
        detail=",".join(org.granted_modules) or "(none)",
        actor_account_id=account.id,
    )
    return org


def sync_ceiling_to_subscription(db: Session, account: Account) -> None:
    """Move a personal workspace's ceiling to match what Stripe last said.

    This is what makes subscribing DO something now that the ceiling is a
    personal org's entitlement. Without it a new subscriber would get
    accounts.role='premium' and see no new modules at all.

    Refuses to touch an org whose ceiling_managed_by is 'grant'. That is the
    comp: staff raise a client's ceiling by hand, the column flips, and no
    webhook can ever take it back. Same asymmetry as granted_by, and the reason
    a comped client does not silently lose access the first time an unrelated
    billing event fires.

    Caller commits.
    """
    org = personal_org_for(db, account.id)
    if org is None:
        return
    if org.ceiling_managed_by != GRANTED_BY_SUBSCRIPTION:
        logger.info(
            "[BILLING] org %s ceiling is staff-granted — leaving it alone", org.id)
        return

    target = ceiling_for_account(account)
    if list(org.granted_modules or []) == target:
        return

    org.granted_modules = target
    audit.record_org_change(
        db, event_type=audit.ORG_CEILING_CHANGE, org_id=org.id,
        detail=",".join(target) or "(none)",
        actor_account_id=account.id,
    )
    logger.info("[BILLING] org %s ceiling -> %s (subscription %s)",
                org.id, target, account.subscription_status)


def default_tier_for(account: Account) -> tuple[str, str]:
    """Retained for callers that still ask. A personal workspace's member is
    its owner, so the tier is inert there — the ceiling is the entitlement."""
    if account.role == "admin":
        return TIER_ORG_OWNER, GRANTED_BY_GRANT
    if account.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES:
        return TIER_PREMIUM, GRANTED_BY_SUBSCRIPTION
    return TIER_FREE, GRANTED_BY_GRANT
