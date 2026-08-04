"""
The tenancy predicate. ONE definition, used by every scoped query.

This is the most security-sensitive expression in the codebase. There is no
row-level security behind it — see the plan's rationale — so a query that
scopes itself by hand and gets it wrong returns another tenant's rows with no
error, no log line, and no symptom until someone notices.

Hence: do not inline `X.org_id == ctx.org_id` at a call site. Use these
helpers, so there is exactly one place to review and one place to fix.

    from src.services.org_scope import scoped
    query = scoped(db, Contact, ctx)              # instead of db.query(Contact)

or, when adding to a query you already have:

    query = query.filter(tenant_predicate(Contact, ctx))

CANONICAL FILE. Mirrored into cmdlabs-agent-api via ./sync-schemas.sh.
"""
from dataclasses import dataclass

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

# Resource types that can be published through the catalog. Deliberately
# narrow — CRM rows are tenant data and may never appear in a catalog.
AGENT = 'agent'
VECTOR_STORE = 'vector_store'


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
    for platform staff, not for any resource type. Staff read another org's
    data by joining it, which leaves a membership row that org can see.

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


def catalog_resource_ids(db: Session, ctx, resource_type: str):
    """IDs of platform-published resources this caller can reach.

    Publishing is one-directional: a catalog item may only reference a
    resource owned by the PLATFORM org, so this can never surface another
    tenant's rows. It is additive only.

    A grant with group_id NULL reaches the whole org; with group_id set it
    reaches only members of that access group — which is how one lesson goes
    to Sales and not Engineering.
    """
    from src.db.catalog_models import CatalogGrant, CatalogItem
    from src.db.models import AccessGroupMember

    my_groups = (
        db.query(AccessGroupMember.access_group_id)
        .filter(AccessGroupMember.account_id == ctx.account_id)
        .subquery()
    )

    return (
        db.query(CatalogItem.resource_id)
        .join(CatalogGrant, CatalogGrant.catalog_item_id == CatalogItem.id)
        .filter(
            CatalogItem.resource_type == resource_type,
            CatalogGrant.org_id == ctx.org_id,
            or_(
                CatalogGrant.group_id.is_(None),
                CatalogGrant.group_id.in_(db.query(my_groups)),
            ),
        )
    )


def visible_resource_predicate(db: Session, model, ctx, resource_type: str,
                               granted_ids=None):
    """Everything a caller may reach for a publishable resource type.

    Three additive arms, each of which can only ever WIDEN, never cross the
    tenant boundary:

      1. resource_predicate — own org, honouring visibility;
      2. explicit AccessGrant ids — an individual or a department inside the
         same org (the caller resolves these; C6 enforces same-org);
      3. the catalog — lessons published by the PLATFORM org to this org or
         to one of the caller's groups.

    Arm 3 is safe precisely because a catalog item may only reference a
    platform-owned resource, so it cannot surface another tenant's row.

    Deliberately not a flag on tenant_predicate: the CRM tables must never be
    able to acquire a catalog arm by someone passing the wrong argument.
    """
    arms = [resource_predicate(model, ctx)]
    if granted_ids:
        arms.append(model.id.in_(granted_ids))
    arms.append(model.id.in_(catalog_resource_ids(db, ctx, resource_type)))
    return or_(*arms)


def scoped_resources(db: Session, model, ctx, resource_type: str, granted_ids=None):
    """`db.query(model)` scoped to everything the caller may reach."""
    return db.query(model).filter(
        visible_resource_predicate(db, model, ctx, resource_type, granted_ids)
    )
