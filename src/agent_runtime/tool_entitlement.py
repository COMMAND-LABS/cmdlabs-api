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

The derivation itself — ceiling, owner bypass, role intersection — is
src/services/modules.py's, called with a minimal ctx rather than re-implemented.
This file used to carry a deliberate copy of that logic back when the agent
runtime was a separate service; since the merge both live in one repo, and two
copies of "what does premium include" is exactly the drift the copy existed to
prevent. What remains here is only what the request context cannot provide:
resolving the membership row, because tools run outside the request and there
is no OrgContext to reuse.

This is the MODULE axis only. It decides which tools exist; org_scope's
tenant_predicate still decides which rows those tools see. Neither substitutes
for the other — a tool that is entitled but unscoped would still be a leak.
"""
import logging
from types import SimpleNamespace

from sqlalchemy.orm import Session

from src.db.models import Organization, OrganizationMember
from src.services import modules

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


def effective_modules(db: Session, account_id: int, org_id: int) -> set[str]:
    """Module keys `account_id` may open in `org_id`.

    Returns an empty set when the account is not a member of the org — the
    caller then builds no gated tools at all, which is the right failure
    direction for a check that runs outside the request context.

    Only the membership lookup lives here; the ceiling / owner-bypass / role
    derivation is modules.effective_modules, shared with the HTTP surface.
    is_owner is DERIVED from the org's owner column rather than a flag on the
    membership row — one fact, one home, matching deps.py. The agent runtime
    never acts as a super admin, so that bypass is pinned off.
    """
    row = (
        db.query(Organization.owner_account_id, OrganizationMember.role)
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

    owner_account_id, role = row
    ctx = SimpleNamespace(
        org_id=org_id,
        role=role,
        is_owner=owner_account_id is not None and owner_account_id == account_id,
        is_super_admin=False,
    )
    return set(modules.effective_modules(db, ctx))


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
