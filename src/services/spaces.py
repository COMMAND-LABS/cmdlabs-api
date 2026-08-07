"""
Space membership: the one thing that decides who may open a space's content.

THE RULE, IN ONE LINE
---------------------
    A space's content is reachable by its MEMBERS. Never by its owner's org.

`Space.owner_org_id` exists for billing and accountability and is never read
here. If it were — if a space's content were reachable because you happen to be
in the org that owns the space — then a member from another org would be
reading rows outside their tenant, and `org_id == ctx.org_id` would need an
exception. A tenancy rule with exceptions is one nobody can verify.

So this module has exactly one job: turn an account into the set of spaces it
belongs to, and answer membership questions about a single space. Everything
that gates space content goes through these functions, for the same reason
every org-scoped query goes through org_scope.tenant_predicate — one place to
review, one place to fix.

WHY IT IS NOT PART OF OrgContext
--------------------------------
The org context is resolved per request and answers "which tenant's rows may
this request see". Space membership is a different question with a different
answer set: an account is in ONE org per request and may be in MANY spaces at
once. Folding the second into the first would produce a context whose org_id
sometimes meant "the tenant" and sometimes meant "one of several containers",
which is precisely the ambiguity the single tenancy rule exists to avoid.
"""
import logging

from sqlalchemy.orm import Session

from src.db.space_models import (
    GRANTED_BY_GRANT,
    JOIN_OPEN,
    JOIN_REQUEST,
    SPACE_ACTIVE,
    TIER_MEMBER,
    TIER_OWNER,
    Space,
    SpaceMember,
    SpaceTier,
)
from src.services import audit

logger = logging.getLogger(__name__)



def space_ids_for(db: Session, account_id: int) -> set:
    """Every space this account belongs to.

    A set rather than a query, because callers use it to build an IN clause
    alongside their own org predicate, and a subquery there would silently
    change the plan of the dual-homed content queries this exists to serve.
    """
    rows = (db.query(SpaceMember.space_id)
              .filter(SpaceMember.account_id == account_id).all())
    return {r[0] for r in rows}


def membership(db: Session, space_id: int, account_id: int) -> SpaceMember | None:
    return (db.query(SpaceMember)
              .filter(SpaceMember.space_id == space_id,
                      SpaceMember.account_id == account_id)
              .first())


def is_member(db: Session, space_id: int, account_id: int) -> bool:
    return membership(db, space_id, account_id) is not None


def is_owner(db: Session, space_id: int, account_id: int) -> bool:
    """Owners administer a space. Deliberately read from the MEMBERSHIP row.

    Not from Space.owner_account_id: that column is attribution and survives
    the account being deleted, so trusting it would let a removed owner keep
    administering a space they are no longer in.
    """
    member = membership(db, space_id, account_id)
    return bool(member and member.is_owner)


def visible_space(db: Session, space_id: int, account_id: int) -> Space | None:
    """A space this account may LOOK at: one they are in, or a discoverable one.

    Looking is not entering. A discoverable space returns its name and
    description to anybody, because that is what a browse page is for; its
    CONTENT is gated by membership, which is a different question asked by a
    different function.
    """
    space = db.query(Space).filter(Space.id == space_id).first()
    if space is None:
        return None
    if space.discoverable and space.status == SPACE_ACTIVE:
        return space
    return space if is_member(db, space_id, account_id) else None



def create_space(db: Session, *, name: str, description: str | None,
                 owner_account_id: int, owner_org_id: int,
                 discoverable: bool, join_policy: str) -> Space:
    """A space, with its creator as its first member and owner.

    `owner_org_id` is recorded and never consulted for access — it answers who
    is accountable and who gets billed. The creator's ACCESS comes from the
    SpaceMember row created here, exactly like everybody else's.

    Two tiers are seeded for the same reason an org gets them: turning a space
    into something you charge for should be setting a price, not first
    discovering the tier list is empty. Caller commits.
    """
    space = Space(
        name=name, description=description,
        owner_account_id=owner_account_id, owner_org_id=owner_org_id,
        discoverable=discoverable, join_policy=join_policy,
        status=SPACE_ACTIVE,
    )
    db.add(space)
    db.flush()

    db.add(SpaceTier(space_id=space.id, tier_key=TIER_OWNER, label="Owner"))
    db.add(SpaceTier(space_id=space.id, tier_key=TIER_MEMBER, label="Member",
                     description="Everyone who has been let in."))
    db.flush()

    add_member(db, space=space, account_id=owner_account_id,
               tier_key=TIER_OWNER, is_owner=True,
               granted_by=GRANTED_BY_GRANT, actor_account_id=owner_account_id)

    audit.record_space(db, event_type=audit.SPACE_CREATE, space_id=space.id,
                       space_name=space.name,
                       detail=f"{join_policy}, "
                              f"{'discoverable' if discoverable else 'private'}",
                       actor_account_id=owner_account_id)
    return space


def add_member(db: Session, *, space: Space, account_id: int, tier_key: str,
               is_owner: bool = False, granted_by: str = GRANTED_BY_GRANT,
               actor_account_id: int | None = None) -> SpaceMember:
    """Put an account in a space. Idempotent. Caller commits.

    Every door — invited, approved, purchased — lands here and writes the same
    shape of row, differing only in `granted_by`. That is what makes "who can
    see this, and how did they get in?" a single query rather than a
    reconstruction from three tables.
    """
    existing = membership(db, space.id, account_id)
    if existing:
        return existing

    member = SpaceMember(
        space_id=space.id, account_id=account_id, tier_key=tier_key,
        is_owner=is_owner, granted_by=granted_by,
        invited_by_account_id=(actor_account_id
                               if actor_account_id != account_id else None),
    )
    db.add(member)
    db.flush()

    audit.record_space(db, event_type=audit.SPACE_MEMBER_ADD, space_id=space.id,
                       space_name=space.name, account_id=account_id,
                       tier_key=tier_key, detail=granted_by,
                       actor_account_id=actor_account_id)
    return member


def remove_member(db: Session, *, space: Space, account_id: int,
                  actor_account_id: int | None = None) -> bool:
    """Take somebody out. Takes effect on their next request. Caller commits."""
    member = membership(db, space.id, account_id)
    if member is None:
        return False

    db.delete(member)
    audit.record_space(db, event_type=audit.SPACE_MEMBER_REMOVE,
                       space_id=space.id, space_name=space.name,
                       account_id=account_id, tier_key=member.tier_key,
                       actor_account_id=actor_account_id)
    return True


def owner_count(db: Session, space_id: int) -> int:
    return (db.query(SpaceMember)
              .filter(SpaceMember.space_id == space_id,
                      SpaceMember.is_owner.is_(True)).count())


def accepts_requests(space: Space) -> bool:
    return space.join_policy in (JOIN_REQUEST, JOIN_OPEN)
