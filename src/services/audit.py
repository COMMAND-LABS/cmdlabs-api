"""
The organization audit log.

One chronological answer to "what happened in this org, and who did it".

Writes to access_grant_events. The table name is now a misnomer — it started
life recording only resource grants — but keeping one table is the point: two
logs means two queries and two chances to look in the wrong one.

WHAT MAKES IT TRUSTWORTHY
-------------------------
Human-readable context is SNAPSHOTTED at write time (actor_email,
principal_label, resource_label) and the id columns carry NO foreign keys. So
an entry stays readable after the account, group, org or resource it describes
is renamed or deleted — which is exactly the entry you needed. A log that goes
blank when its subject is removed documents only the uninteresting cases.

Recording is best-effort and must never fail the operation it describes: an
audit write that can 500 a request is an audit write people delete. Failures
are logged loudly instead.
"""
import logging

from sqlalchemy.orm import Session

from src.db.models import AccessGrantEvent, Account, Organization

logger = logging.getLogger(__name__)

# ── vocabulary ───────────────────────────────────────────────────────────────
# Kept in step with the CHECK constraint on access_grant_events (migration
# c1d2e3f4a5b6). The original three are unprefixed because rows written before
# the log was widened still hold them.

GRANT_CREATE = "create"
GRANT_REVOKE = "revoke"
GRANT_ROLE_CHANGE = "role_change"

MEMBER_ADD = "member.add"
MEMBER_REMOVE = "member.remove"
MEMBER_TIER_CHANGE = "member.tier_change"

ORG_CREATE = "org.create"
ORG_SUSPEND = "org.suspend"
ORG_RESTORE = "org.restore"
ORG_CEILING_CHANGE = "org.ceiling_change"
ORG_RENAME = "org.rename"

TIER_MODULES_CHANGE = "tier.modules_change"

CATALOG_PUBLISH = "catalog.publish"
CATALOG_UNPUBLISH = "catalog.unpublish"
CATALOG_GRANT = "catalog.grant"
CATALOG_REVOKE = "catalog.revoke"

# Platform staff joining a tenant in order to read its data. This is what makes
# "our staff cannot read your data without appearing in your member list" a
# claim a customer can check rather than one they have to take on trust.
STAFF_JOIN = "staff.join"

# Resource types beyond the original agent | vector_store | credential.
RESOURCE_ORGANIZATION = "organization"
RESOURCE_MEMBERSHIP = "membership"
RESOURCE_TIER = "tier"
RESOURCE_CATALOG_ITEM = "catalog_item"


def _actor_email(db: Session, actor_account_id: int | None) -> str | None:
    if actor_account_id is None:
        return None
    return db.query(Account.email).filter(Account.id == actor_account_id).scalar()


def record(
    db: Session,
    *,
    event_type: str,
    org_id: int | None,
    resource_type: str,
    resource_id: int,
    resource_label: str | None = None,
    principal_type: str | None = None,
    principal_id: int | None = None,
    principal_label: str | None = None,
    role: str | None = None,
    detail: str | None = None,
    actor_account_id: int | None = None,
) -> AccessGrantEvent | None:
    """Append one event. Caller commits.

    Never raises. An audit failure must not fail the operation being audited —
    but it is logged at ERROR so it cannot pass unnoticed either.
    """
    try:
        event = AccessGrantEvent(
            org_id=org_id,
            event_type=event_type,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_label=resource_label,
            principal_type=principal_type,
            principal_id=principal_id,
            principal_label=principal_label,
            role=role,
            detail=detail,
            actor_account_id=actor_account_id,
            actor_email=_actor_email(db, actor_account_id),
        )
        db.add(event)
        return event
    except Exception:
        logger.exception(
            "[AUDIT] Failed to record %s on %s %s in org %s",
            event_type, resource_type, resource_id, org_id,
        )
        return None


# ── convenience wrappers ─────────────────────────────────────────────────────
# Each snapshots the labels its event needs, so no caller has to remember which
# ones matter for which verb.

def record_membership(db: Session, *, event_type: str, org_id: int,
                      account_id: int, tier_key: str | None = None,
                      actor_account_id: int | None = None) -> None:
    org_name = db.query(Organization.name).filter(Organization.id == org_id).scalar()
    email = db.query(Account.email).filter(Account.id == account_id).scalar()
    record(
        db,
        event_type=event_type,
        org_id=org_id,
        resource_type=RESOURCE_ORGANIZATION,
        resource_id=org_id,
        resource_label=org_name,
        principal_type="account",
        principal_id=account_id,
        principal_label=email,
        role=tier_key,
        actor_account_id=actor_account_id,
    )


def record_org_change(db: Session, *, event_type: str, org_id: int,
                      detail: str | None = None,
                      actor_account_id: int | None = None) -> None:
    """An org-level change with no counterparty — a ceiling edit, a suspension.

    `detail` records what it BECAME (for a ceiling, the resulting module list),
    so the log answers "what changed to what" rather than merely "something
    changed". It has its own unbounded column: an early version squeezed this
    into `role`, a String(20), and the first real ceiling change overflowed it.
    """
    org_name = db.query(Organization.name).filter(Organization.id == org_id).scalar()
    record(
        db,
        event_type=event_type,
        org_id=org_id,
        resource_type=RESOURCE_ORGANIZATION,
        resource_id=org_id,
        resource_label=org_name,
        detail=detail,
        actor_account_id=actor_account_id,
    )


def record_catalog(db: Session, *, event_type: str, item_id: int,
                   title: str | None, target_org_id: int | None = None,
                   group_id: int | None = None,
                   actor_account_id: int | None = None) -> None:
    """A publish/unpublish, or a grant/revoke to one org (or department).

    org_id is the org RECEIVING the lesson, so a client can see in their own
    log when a lesson arrived and when it was taken away. Publish and unpublish
    have no recipient and so carry no org.
    """
    record(
        db,
        event_type=event_type,
        org_id=target_org_id,
        resource_type=RESOURCE_CATALOG_ITEM,
        resource_id=item_id,
        resource_label=title,
        principal_type="group" if group_id else None,
        principal_id=group_id,
        actor_account_id=actor_account_id,
    )
