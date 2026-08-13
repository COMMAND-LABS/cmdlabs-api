"""
Module entitlement for the AGENT RUNTIME.

cmdlabs-api gates its HTTP surface with require_module(): a member whose role
excludes Contacts gets a 404 from /api/contacts. That closes the front door
only. An agent's tools read the same tables from this service, over their own
sessions, and knew nothing about modules — so the same member could ask an
agent to list their contacts and get them.

This closes that: a tool whose module the caller cannot open is never built, so
it is not in the model's tool list at all. Absent rather than refusing, which
is both cheaper and quieter — the model does not narrate a capability the
caller was not sold.

    effective = the org's PLAN  ∩  the member's ROLE

kept deliberately identical to cmdlabs-api/src/services/modules.py, including
the owner bypass. Two services enforcing entitlement differently is worse than
one enforcing it and one not: the gap is harder to see.

The ceiling is DERIVED, never read from a column. It used to read
organizations.granted_modules directly, and when that column became a pinned
PLAN this file would have resolved every org to an empty set — every gated tool
silently absent from every agent. It reads config/plans_registry, which is
byte-synced from cmdlabs-api (check-schemas.sh) precisely so "what does premium
include" cannot be answered twice.

This is the MODULE axis only. It decides which tools exist; org_scope's
tenant_predicate still decides which rows those tools see. Neither substitutes
for the other — a tool that is entitled but unscoped would still be a leak.
"""
import logging

from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.config import roles_registry as roles
from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
)

logger = logging.getLogger(__name__)

# Which module each registered tool type belongs to.
#
# A tool type absent from this map is NOT gated. That is deliberate for
# infrastructure-ish tools (raw DB read/write, which are bound to a credential
# the caller already had to hold), and it is why the map lists every registered
# type explicitly rather than only the gated ones — an unlisted type should be
# an oversight you can see, not a silent default.
TOOL_MODULES = {
    "vectorSearch": "knowledge_bases",
    "vectorSearchWithReranking": "knowledge_bases",
    "contactRead": "contacts",
    "contactEventsRead": "contacts",
    "contactEventWrite": "contacts",
    "sendTxtEmailWithSes": "email_campaigns",
    "sendHtmlEmailWithSes": "email_campaigns",
    # Ungated: bound to a credential grant rather than to a product module.
    "dbTableRead": None,
    "dbTableWrite": None,
}


def _ceiling(db: Session, pinned_plan, owner_account_id) -> list[str]:
    """The modules this org's PLAN opens. Mirrors modules.org_entitlement.

    A pinned plan wins outright — that is the comp, and billing may never undo
    it. Otherwise the plan is read from the owner's subscription, including the
    grace window after a lapse (which keeps the paid modules and refuses writes
    on the HTTP side; an agent tool is a read, so nothing here changes).
    """
    if pinned_plan is not None:
        return plans.modules_for_plan(pinned_plan)
    if owner_account_id is None:
        return plans.modules_for_plan(plans.PLAN_FREE)

    owner = (db.query(Account.subscription_status,
                      Account.subscription_lapsed_at)
               .filter(Account.id == owner_account_id).first())
    if owner is None:
        return plans.modules_for_plan(plans.PLAN_FREE)
    return plans.modules_for_plan(plans.plan_for(owner[0], owner[1]))


def effective_modules(db: Session, account_id: int, org_id: int) -> set[str]:
    """Module keys `account_id` may open in `org_id`.

    Returns an empty set when the account is not a member of the org — the
    caller then builds no gated tools at all, which is the right failure
    direction for a check that runs outside the request context.
    """
    row = (
        db.query(Organization.pinned_plan, Organization.owner_account_id,
                 OrganizationMember.role)
        .join(OrganizationMember, OrganizationMember.org_id == Organization.id)
        .filter(Organization.id == org_id,
                OrganizationMember.account_id == account_id)
        .first()
    )
    if row is None:
        logger.warning(
            "[TOOLS] account %s is not a member of org %s — no gated tools",
            account_id, org_id,
        )
        return set()

    pinned_plan, owner_account_id, role = row
    ceiling = set(_ceiling(db, pinned_plan, owner_account_id))

    # An owner reaches their org's whole ceiling regardless of their own role,
    # matching cmdlabs-api. Platform super admins likewise bypass the role but
    # not the ceiling of the org they are acting in.
    #
    # DERIVED from the org's owner column, matching cmdlabs-api deps.py. This
    # used to read a per-membership is_owner flag, which was a second copy of
    # the same fact; owner_account_id is already selected above for the
    # ceiling, so asking it costs nothing and cannot disagree.
    if owner_account_id is not None and owner_account_id == account_id:
        return ceiling

    # No second query: a role is a constant, so what it opens is decided in
    # process. This used to fetch organization_tiers.modules per tool build.
    return set(roles.modules_for(role, sorted(ceiling)))


def allowed_tool_configs(tool_configs: list, granted: set[str]) -> list:
    """Drop tool configs whose module the caller cannot open."""
    kept = []
    for cfg in tool_configs:
        module = TOOL_MODULES.get(cfg.get("type"))
        if module is not None and module not in granted:
            logger.info(
                "[TOOLS] dropping tool %r — %s not enabled for this caller",
                cfg.get("type"), module,
            )
            continue
        kept.append(cfg)
    return kept
