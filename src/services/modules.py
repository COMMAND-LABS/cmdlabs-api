"""
Module entitlement: which screens a caller may open.

    effective = org.granted_modules  ∩  tier.modules

Two levels, and only two:

  - the CEILING (organizations.granted_modules) — what platform staff allow
    this org to use at all. Bespoke per org; there is no plan table and no
    shared template.
  - the TIER (organization_tiers.modules) — how the org owner distributes a
    subset of that ceiling among their own tiers.

TIERS ARE NOT LEVELS. Each tier is an arbitrary set of module keys; nothing
requires 'premium' to be a superset of 'free', and two tiers may be completely
disjoint. The only relationship in the system is the intersection above, which
is a cap rather than a hierarchy.

INTERSECTED AT READ TIME, never cascaded. Lowering a ceiling takes effect on
the caller's next request without rewriting any tier row — so a revocation can
never be half-applied, and there is no stale grant left behind to re-widen
access later.

Distinct from the DATA axis: modules decide which screens open, org_id decides
which rows are visible. A misconfigured tier is a wrong menu; it can never be a
data leak.
"""
import logging
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config.modules_registry import MODULE_KEYS, normalize
from src.db.models import Account, Organization, OrganizationTier

# Mirrors services.organizations.GRANTED_BY_SUBSCRIPTION. Duplicated rather
# than imported: this file is read on every request and importing the service
# layer from it would invert the dependency direction.
GRANTED_BY_SUBSCRIPTION = "subscription"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OrgEntitlement:
    """What an org may open, and whether it may be changed.

    ONE OBJECT because it is one derivation. Both answers come from the same
    two facts — the org's ceiling_managed_by, and the owner's billing — and
    computing them separately is how they end up disagreeing: an org showing
    premium modules while refusing every write, or the reverse.
    """
    ceiling: list
    # True during the grace window after a lapse: the modules stay, the writes
    # stop. Never true for a comped org — see below.
    read_only: bool
    # When read-only turns into a downgrade to free. None when nothing lapsed.
    grace_ends_at: datetime | None


def org_entitlement(db: Session, org_id: int,
                    now: datetime | None = None) -> OrgEntitlement:
    """What this org may use, and whether it may write.

    DERIVED WHEN BILLING OWNS IT, STORED WHEN A HUMAN DOES. One column was
    doing two jobs:

        ceiling_managed_by='subscription'  a CACHE of the owner's plan
        ceiling_managed_by='grant'         bespoke, set by staff, authoritative

    Only the second needs storing. The first was a copy of a dict lookup, and a
    copy with no invalidation path is a value that drifts — which is exactly
    what happened three times: adding `courses`, then `spaces`, then discovering
    the platform org had missed both. Each needed a hand-written backfill
    migration, and each was found by somebody noticing a menu item was absent.

    Computed here, editing config/plans_registry.PLAN_MODULES takes effect on
    everybody's next request. There is no backfill to forget.

    A GRANT IS NEVER RECOMPUTED, AND NEVER GOES READ-ONLY. Staff raising a
    client's ceiling by hand is a promise, and this function must not quietly
    withdraw it — same asymmetry OrganizationMember.granted_by encodes one
    level down. A comped org has no subscription to lapse, so billing has
    nothing to say about it in either direction.
    """
    row = (db.query(Organization.granted_modules,
                    Organization.ceiling_managed_by,
                    Organization.owner_account_id)
             .filter(Organization.id == org_id).first())
    if row is None:
        return OrgEntitlement(ceiling=[], read_only=False, grace_ends_at=None)

    granted, managed_by, owner_account_id = row
    if managed_by != GRANTED_BY_SUBSCRIPTION:
        return OrgEntitlement(ceiling=normalize(granted), read_only=False,
                              grace_ends_at=None)

    # Billing owns it: read the plan rather than the copy of it. An org with no
    # owner (the account was deleted) falls back to what was last stored, which
    # is the conservative answer — never wider than it already was, and never
    # read-only, since there is nobody who could fix the payment.
    if owner_account_id is None:
        return OrgEntitlement(ceiling=normalize(granted), read_only=False,
                              grace_ends_at=None)

    owner = (db.query(Account.subscription_status,
                      Account.subscription_lapsed_at)
               .filter(Account.id == owner_account_id).first())
    if owner is None:
        return OrgEntitlement(ceiling=normalize(granted), read_only=False,
                              grace_ends_at=None)

    subscription_status, lapsed_at = owner
    state = plans.billing_state(subscription_status, lapsed_at, now)
    return OrgEntitlement(
        ceiling=plans.modules_for_plan(plans.plan_for_state(state)),
        read_only=(state == plans.BILLING_GRACE),
        grace_ends_at=(plans.grace_ends_at(lapsed_at)
                       if state == plans.BILLING_GRACE else None),
    )


def ceiling_for(db: Session, org_id: int) -> list:
    """What this org may use at all. See org_entitlement."""
    return org_entitlement(db, org_id).ceiling


def tier_modules(db: Session, org_id: int, tier_key: str) -> list:
    row = (
        db.query(OrganizationTier.modules)
        .filter(OrganizationTier.org_id == org_id,
                OrganizationTier.tier_key == tier_key)
        .first()
    )
    return normalize(row[0] if row else [])


def effective_modules(db: Session, ctx) -> list:
    """The modules `ctx` may open, in registry order.

    An OWNER gets the org's whole ceiling rather than their tier's subset. That
    is a bypass, not a stored grant: if an owner's own tier could be edited
    down, one bad save in the matrix would lock them out of the very screen
    that undoes it.

    PLATFORM STAFF bypass both layers and get every module that exists.

    That is wider than it was — staff used to be capped by the ceiling of the
    org they were acting in — and the reason is that the cap never did any
    work. It only meant staff had to be sitting in an org whose ceiling
    happened to be full, which is the entire job the platform org existed to
    do. Removing the cap removes the need for a special org.

    Safe because of the axis this file is about: modules decide which SCREENS
    open, org_id decides which ROWS are visible. Staff reading a tenant's data
    still requires JOINING that tenant, which leaves a membership row its
    members can see and a staff.join entry naming who and when. The widest this
    goes is staff opening a screen for an org that never bought that module,
    where they find that org's own (empty) rows.
    """
    if getattr(ctx, "is_super_admin", False):
        return list(MODULE_KEYS)

    ceiling = ceiling_for(db, ctx.org_id)

    if getattr(ctx, "is_owner", False):
        return ceiling

    granted = set(tier_modules(db, ctx.org_id, ctx.tier_key))
    return [k for k in ceiling if k in granted]


def can_open(db: Session, ctx, module_key: str) -> bool:
    return module_key in set(effective_modules(db, ctx))


def clamp_to_ceiling(db: Session, org_id: int, requested) -> list:
    """A tier may only ever name modules inside its org's ceiling.

    Silently dropping the excess (rather than rejecting the request) keeps the
    matrix usable while a ceiling is being lowered: the owner's save succeeds
    and simply cannot exceed what they were given. The UI shows out-of-ceiling
    modules as disabled with the reason, so nothing is mysterious.
    """
    ceiling = set(ceiling_for(db, org_id))
    asked = set(normalize(requested))
    dropped = asked - ceiling
    if dropped:
        logger.info(
            "[MODULES] org %s: dropped %s from a tier — outside the org ceiling",
            org_id, sorted(dropped),
        )
    return [k for k in MODULE_KEYS if k in asked and k in ceiling]
