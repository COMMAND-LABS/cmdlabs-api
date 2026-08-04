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

from sqlalchemy.orm import Session

from src.config.modules_registry import MODULE_KEYS, normalize
from src.db.models import Organization, OrganizationTier

logger = logging.getLogger(__name__)


def ceiling_for(db: Session, org_id: int) -> list:
    org = db.query(Organization.granted_modules).filter(
        Organization.id == org_id).first()
    return normalize(org[0] if org else [])


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

    Platform staff likewise bypass the tier, but NOT the ceiling of the org
    they are acting in — staff see every screen, and still only this org's
    rows.
    """
    ceiling = ceiling_for(db, ctx.org_id)

    if getattr(ctx, "is_owner", False) or getattr(ctx, "is_super_admin", False):
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
