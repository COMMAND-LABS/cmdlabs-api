"""
The tenancy predicate. ONE definition, used by every scoped query.

This is the most security-sensitive expression in the codebase. There is no
row-level security behind it — see the plan's rationale — so a query that
scopes itself by hand and gets it wrong returns another tenant's rows with no
error, no log line, and no symptom until someone notices.

Hence: do not inline `X.org_id == ctx.org_id` at a call site. Use these
helpers, so there is exactly one place to review and one place to fix.

Fetching ONE row by id — by far the most common scoped read:

    from src.services.org_scope import get_scoped_or_404
    contact = get_scoped_or_404(db, Contact, contact_id, ctx)

Listing:

    from src.services.org_scope import scoped
    query = scoped(db, Contact, ctx)              # instead of db.query(Contact)

or, when adding to a query you already have:

    query = query.filter(tenant_predicate(Contact, ctx))

"""
import re
from dataclasses import dataclass

from fastapi import HTTPException, status
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session


@dataclass(frozen=True)
class OrgScope:
    """The minimum a query needs to scope itself to a tenant.

    cmdlabs-api resolves a richer OrgContext per request (tier, ownership,
    status); this is the subset the predicates below actually read, and it is
    what the agent runtime carries into tools.

    It exists because agent tools do NOT run on the request session — the
    request session is closed before the agent loop starts and each tool opens
    its own. There is therefore no ambient context to inherit, and the scope
    has to be passed explicitly. A shared shape keeps both services scoping
    identically rather than approximately.

    `account_id` is here for attribution and for resource visibility, NOT for
    row scoping. Once every account had its own org, org_id became sufficient.
    """
    account_id: int
    org_id: int

# Tables whose "who created this" column is not called account_id.
_CREATED_BY_COLUMN = {
    'vector_stores': 'owner_account_id',
}

# Resource types that are SHARABLE — the ones a grant may name. Deliberately
# narrow, and it stays narrow: CRM rows are tenant data and may never become
# something one account hands to another.
AGENT = 'agent'
VECTOR_STORE = 'vector_store'
# Skills are resource-shaped (org_id + visibility) from day one so per-person
# grants are an additive change; no AccessGrant names a skill yet.
SKILL = 'skill'


def created_by_column(model):
    """The column recording who created a row.

    Since org scoping landed, account_id is ATTRIBUTION, not the tenant key —
    it records who made the row and no longer decides who sees it. It does
    still narrow RESOURCES (agents, knowledge bases), which default to private
    within their org until deliberately marked 'org'.
    """
    name = _CREATED_BY_COLUMN.get(model.__tablename__, 'account_id')
    return getattr(model, name)


def tenant_predicate(model, ctx):
    """Rows of `model` that `ctx` may see. One clause, no exceptions.

    org_id must match. That is the entire tenancy rule — not for owners, not
    for platform super admins, not for any resource type. Super admins read
    another org's data by joining it, which leaves a membership row that org
    can see.

    This used to carry a second clause: inside a 'personal' org a member saw
    only rows they created. That existed because the root org held every signup
    at once, so it needed a rule for strangers sharing one org. Since
    e3f4a5b6c7d8 every account owns its own org and a team is just an org with
    more members, so the clause was constant-true everywhere and f4a5b6c7d8e9
    removed the column behind it.

    Worth keeping it this way. This expression is the one thing standing
    between two tenants — there is no row-level security behind it — and its
    reviewability IS the safety property. A single comparison can be checked at
    a glance at all ~40 call sites; a conditional cannot.

    Attribution did not go anywhere: account_id still records who created each
    row, it just no longer decides who sees it.
    """
    return model.org_id == ctx.org_id


def scoped(db: Session, model, ctx):
    """`db.query(model)` with the tenancy predicate already applied."""
    return db.query(model).filter(tenant_predicate(model, ctx))


def _default_label(model) -> str:
    """'ContactList' -> 'Contact list'. The noun a 404 message starts with.

    Only the first word is capitalized, which is what the hand-written messages
    already said at most call sites. Where a route said something else ("Event
    not found" for a ContactEvent), it passes `label` explicitly rather than
    letting this guess — the message is part of the API surface and should not
    shift because a model was renamed.
    """
    words = re.findall(r'[A-Z][a-z0-9]*', model.__name__) or [model.__name__]
    return ' '.join([words[0]] + [w.lower() for w in words[1:]])


def get_scoped_or_404(db: Session, model, obj_id, ctx, *extra, label=None):
    """Fetch ONE row of `model` by id inside `ctx`'s tenant, or raise 404.

    THE LOOKUP, not just the predicate. tenant_predicate above gives one place
    to review the tenancy EXPRESSION, but until this existed every route still
    assembled the query around it by hand — ~50 near-identical copies of

        row = db.query(M).filter(M.id == x, tenant_predicate(M, ctx)).first()
        if not row:
            raise HTTPException(404, "M not found")

    and the failure mode of getting one wrong is the one this module's header
    warns about: no error, no log line, just another tenant's row rendering as
    if it were yours. Making the whole fetch a single call means a scoped
    read-by-id cannot be written without its scope.

    404 rather than 403 when the row exists in another org, deliberately and
    for free: the predicate makes it indistinguishable from absent, so this
    never confirms that an id belongs to somebody else.

    `*extra` adds further clauses for a nested read — a contact event is looked
    up by its own id AND its parent contact's:

        get_scoped_or_404(db, ContactEvent, event_id, ctx,
                          ContactEvent.contact_id == contact_id, label="Event")

    Extra clauses can only NARROW. There is no argument that removes the
    tenancy clause, which is the whole point of routing these reads through
    here rather than leaving them as hand-assembled queries.
    """
    row = db.query(model).filter(
        model.id == obj_id,
        tenant_predicate(model, ctx),
        *extra,
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label or _default_label(model)} not found",
        )
    return row


def get_resource_or_404(db: Session, model, obj_id, ctx, *, label=None):
    """The same fetch for a RESOURCE table, honouring `visibility`.

    Separate from get_scoped_or_404 for exactly the reason resource_predicate
    is separate from tenant_predicate: these two must never become one function
    with a flag, or a CRM table acquires a sharing arm the day somebody passes
    the wrong argument.

    Note this arm does NOT consult explicit grants — it is resource_predicate
    alone, which is what the agent routes using it already did. A route that
    also honours grants wants scoped_resources() with granted_ids.
    """
    row = db.query(model).filter(
        model.id == obj_id,
        resource_predicate(model, ctx),
    ).first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{label or _default_label(model)} not found",
        )
    return row


def resource_predicate(model, ctx):
    """Rows of a RESOURCE table (agents, vector stores) that `ctx` may see.

    Distinct from tenant_predicate because these carry a `visibility` column:
    a resource is reachable by everyone in the org only if its creator marked
    it 'org'. A CRM contact belongs to the team by default; an agent someone is
    still building does not.

        org_id == mine
        AND ( visibility == 'org'      -- deliberately shared with the team
              OR created_by == me )    -- always your own

    In a personal org this is moot — its one member created everything in it —
    so the rule needs no special case for workspaces, which is the point of
    every org meaning the same thing.

    This is still narrower than tenant_predicate, deliberately. Resources are
    few and consequential (an agent carries credentials and reaches a knowledge
    base), so they default to private and widening is an explicit act. CRM rows
    are many and belong to the team by nature.

    Explicit grants (AccessGrant) are additive on top of this and are applied
    by the caller — they are how one names an individual or a department.
    """
    return and_(
        model.org_id == ctx.org_id,
        or_(model.visibility == 'org', created_by_column(model) == ctx.account_id),
    )


def visible_resource_predicate(db: Session, model, ctx, resource_type: str,
                               granted_ids=None):
    """Everything a caller may reach for a publishable resource type.

    Two additive arms, neither of which crosses the tenant boundary:

      1. resource_predicate — own org, honouring visibility;
      2. explicit AccessGrant ids — an individual or a department inside the
         same org (the caller resolves these; C6 enforces same-org).

    There was a third arm — resources shared into a SPACE this caller belonged
    to — and it was the only one that crossed an org boundary. Spaces were
    removed to simplify the platform, so today NOTHING here reaches outside
    ctx.org_id. When spaces return, the arm returns here and nowhere else: it
    is the single place a cross-org read is expressible, which is what makes it
    reviewable.

    Deliberately not a flag on tenant_predicate: the CRM tables must never be
    able to acquire a sharing arm by someone passing the wrong argument.

    `db` and `resource_type` are now unused. They are kept because they are the
    parameters the sharing arm reads, and every call site already passes them —
    threading them back through later should not be a signature change.
    """
    arms = [resource_predicate(model, ctx)]
    if granted_ids:
        arms.append(model.id.in_(granted_ids))
    return or_(*arms)


def scoped_resources(db: Session, model, ctx, resource_type: str, granted_ids=None):
    """`db.query(model)` scoped to everything the caller may reach."""
    return db.query(model).filter(
        visible_resource_predicate(db, model, ctx, resource_type, granted_ids)
    )
