"""
Publishing platform content to client organizations.

The catalog is how one lesson, authored once, appears live in many client orgs
— the master updates and every org sees it, with no copy-per-client fan-out.

WHY THIS IS NOT A HOLE IN THE TENANCY BOUNDARY
----------------------------------------------
Direction is the whole argument:

    Acme -> Beta       tenant data moving sideways     never
    Acme -> platform   exfiltration                    never
    platform -> Acme   publishing our own content      fine

A lesson is not tenant data. It is platform content flowing outward, the same
relationship a product has with its own documentation. So "no tenant's data
ever reaches another tenant" is untouched.

`assert_publishable` is the single check that keeps that true. If a
tenant-owned resource could be published, an org owner could publish their own
agent and have it granted to a competitor — and the catalog would become
precisely the cross-tenant channel it exists to avoid. Everything else here is
bookkeeping; that function is load-bearing.
"""
import logging

from sqlalchemy.orm import Session

from src.db.catalog_models import PUBLISHABLE_RESOURCE_TYPES, CatalogGrant, CatalogItem
from src.db.models import AccessGroup, Account, Agent, Organization, VectorStore

logger = logging.getLogger(__name__)

# The org that owns publishable content. Same row as the platform's own tenant:
# the root org is not a special case, it is simply the org CMD LABS operates in.
PLATFORM_ORG_SLUG = "root"


class NotPublishableError(Exception):
    """The resource may not be published — it does not belong to the platform."""


def platform_org(db: Session) -> Organization | None:
    return db.query(Organization).filter(Organization.slug == PLATFORM_ORG_SLUG).first()


def _resource_owner(db: Session, resource_type: str, resource_id: int):
    """(org_id, creator_account_id) for a publishable resource, or (None, None)."""
    if resource_type == "agent":
        row = (db.query(Agent.org_id, Agent.account_id)
                 .filter(Agent.id == resource_id).first())
    elif resource_type == "vector_store":
        row = (db.query(VectorStore.org_id, VectorStore.owner_account_id)
                 .filter(VectorStore.id == resource_id).first())
    else:
        return None, None
    return (row[0], row[1]) if row else (None, None)


def assert_publishable(db: Session, resource_type: str, resource_id: int) -> None:
    """Refuse to publish anything the platform does not own.

    THE check the catalog design rests on. Publishing is one-directional by
    construction: because a CatalogItem may only ever reference a platform-org
    resource, "Acme publishes to Beta" cannot be expressed, and the catalog arm
    in org_scope can only ever add platform content to a tenant's view.

    Also refuses unknown resource types, so the CRM tables can never become
    publishable by someone passing a new string.

    TWO conditions, not one. "Belongs to the platform org" is not sufficient on
    its own, because the platform org IS the root org — the same row that holds
    every public signup. Under an org check alone, any of those accounts' own
    agents would qualify as platform content, and staff could publish a
    stranger's private agent into a client org. So the resource must ALSO have
    been authored by platform staff.

    That makes "a lesson is not tenant data" true rather than merely intended.
    The day the platform gets its own org, distinct from where the public signs
    up, this second condition becomes redundant — and harmless.
    """
    if resource_type not in PUBLISHABLE_RESOURCE_TYPES:
        raise NotPublishableError(
            f"{resource_type!r} is not publishable. Only "
            f"{', '.join(PUBLISHABLE_RESOURCE_TYPES)} may be published — the CRM "
            f"tables hold tenant data and must never enter the catalog."
        )

    org = platform_org(db)
    if org is None:
        raise NotPublishableError("No platform organization exists.")

    resource_org_id, creator_account_id = _resource_owner(db, resource_type, resource_id)
    if resource_org_id is None:
        raise NotPublishableError(f"{resource_type} {resource_id} does not exist.")

    if resource_org_id != org.id:
        logger.warning(
            "[CATALOG] Refused to publish %s %s owned by org %s (platform is %s)",
            resource_type, resource_id, resource_org_id, org.id,
        )
        raise NotPublishableError(
            f"{resource_type} {resource_id} belongs to another organization and "
            f"cannot be published. Publishing only ever flows outward from the "
            f"platform."
        )

    creator_role = (db.query(Account.role)
                      .filter(Account.id == creator_account_id).scalar())
    if creator_role != "admin":
        logger.warning(
            "[CATALOG] Refused to publish %s %s — authored by account %s, who is "
            "not platform staff",
            resource_type, resource_id, creator_account_id,
        )
        raise NotPublishableError(
            f"{resource_type} {resource_id} was not authored by platform staff "
            f"and cannot be published. The root org holds public signups as well "
            f"as platform content, so belonging to it is not by itself proof "
            f"that a resource is ours to publish."
        )


def assert_grantable(db: Session, org_id: int, group_id: int | None) -> None:
    """A group-scoped grant must name a group inside the org being granted to.

    Otherwise a grant could name Acme's org and Beta's 'Sales' group, and
    membership of Beta's group would decide who in Acme sees the lesson.
    """
    org = db.query(Organization.id).filter(Organization.id == org_id).first()
    if org is None:
        raise NotPublishableError(f"Organization {org_id} does not exist.")

    if group_id is None:
        return

    row = db.query(AccessGroup.org_id).filter(AccessGroup.id == group_id).first()
    if row is None or row[0] != org_id:
        raise NotPublishableError(
            f"Group {group_id} does not belong to organization {org_id}."
        )


def publish(db: Session, *, resource_type: str, resource_id: int, title: str,
            description: str | None, published_by_account_id: int) -> CatalogItem:
    """Publish a platform resource, or return the existing entry for it.

    Idempotent by (resource_type, resource_id): publishing twice would create
    two independently-revocable grant surfaces for the same lesson, so
    revoking one would appear to work while access continued through the other.
    """
    assert_publishable(db, resource_type, resource_id)

    existing = (
        db.query(CatalogItem)
        .filter(CatalogItem.resource_type == resource_type,
                CatalogItem.resource_id == resource_id)
        .first()
    )
    if existing:
        existing.title = title
        if description is not None:
            existing.description = description
        return existing

    item = CatalogItem(
        resource_type=resource_type,
        resource_id=resource_id,
        title=title,
        description=description,
        published_by_account_id=published_by_account_id,
    )
    db.add(item)
    return item


def grant_to_org(db: Session, *, item: CatalogItem, org_id: int,
                 group_id: int | None, granted_by_account_id: int) -> CatalogGrant:
    """Grant a published item to an org, or to one group inside it."""
    assert_grantable(db, org_id, group_id)

    existing = (
        db.query(CatalogGrant)
        .filter(CatalogGrant.catalog_item_id == item.id,
                CatalogGrant.org_id == org_id,
                CatalogGrant.group_id.is_(None) if group_id is None
                else CatalogGrant.group_id == group_id)
        .first()
    )
    if existing:
        return existing

    grant = CatalogGrant(
        catalog_item_id=item.id,
        org_id=org_id,
        group_id=group_id,
        granted_by_account_id=granted_by_account_id,
    )
    db.add(grant)
    return grant
