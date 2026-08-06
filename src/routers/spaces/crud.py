"""
Spaces: browse, create, join, and administer.

WHAT THIS ROUTER MAY NEVER DO
-----------------------------
Return tenant data. A space holds shared content and its members come from many
organizations; the moment a space endpoint returns a contact, a deal, or
anything else carrying an org_id, the second container has become a hole in the
first. Everything here is about the space itself and who is in it.

LOOKING vs ENTERING
-------------------
Two different questions, kept apart deliberately:

    LOOK   a discoverable space's name and description, to anybody — that is
           what a browse page is for
    ENTER  its content, to MEMBERS only

So `GET /api/spaces/{id}` answers for a discoverable space you are not in, and
returns nothing about who else is in it. A private space you are not in 404s,
which is also what a nonexistent one returns.
"""
import logging
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func

from src.db.models import Account
from src.db.space_models import (
    GRANTED_BY_GRANT,
    GRANTED_BY_REQUEST,
    JOIN_INVITE,
    JOIN_OPEN,
    REQUEST_APPROVED,
    REQUEST_DENIED,
    REQUEST_PENDING,
    SPACE_ACTIVE,
    TIER_MEMBER,
    Space,
    SpaceJoinRequest,
    SpaceMember,
    SpaceTier,
)
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import audit, spaces
from src.utils.errors import handle_db_error

from .models import (
    CreateSpaceRequest,
    InviteToSpaceRequest,
    JoinRequestBody,
    JoinRequestResponse,
    SpaceDetail,
    SpaceMemberResponse,
    SpaceSummary,
    SpaceTierResponse,
    UpdateSpaceRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter()

NOT_FOUND = HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                          detail="Not found")


def _member_counts(db, space_ids) -> dict:
    if not space_ids:
        return {}
    rows = (db.query(SpaceMember.space_id, func.count(SpaceMember.id))
              .filter(SpaceMember.space_id.in_(space_ids))
              .group_by(SpaceMember.space_id).all())
    return dict(rows)


def _request_statuses(db, account_id: int, space_ids) -> dict:
    if not space_ids:
        return {}
    rows = (db.query(SpaceJoinRequest.space_id, SpaceJoinRequest.status)
              .filter(SpaceJoinRequest.account_id == account_id,
                      SpaceJoinRequest.space_id.in_(space_ids)).all())
    return dict(rows)


def _summary(space: Space, *, member: SpaceMember | None, member_count: int,
             request_status: str) -> SpaceSummary:
    return SpaceSummary(
        id=space.id, slug=space.slug, name=space.name,
        description=space.description, discoverable=space.discoverable,
        join_policy=space.join_policy, status=space.status,
        member_count=member_count,
        is_member=member is not None,
        is_owner=bool(member and member.is_owner),
        request_status=request_status,
        created_at=space.created_at,
    )


def _summaries(db, org, rows: List[Space]) -> List[SpaceSummary]:
    ids = [s.id for s in rows]
    counts = _member_counts(db, ids)
    requests = _request_statuses(db, org.account_id, ids)
    mine = {m.space_id: m for m in db.query(SpaceMember).filter(
        SpaceMember.account_id == org.account_id,
        SpaceMember.space_id.in_(ids or [0]))}
    return [
        _summary(s, member=mine.get(s.id), member_count=counts.get(s.id, 0),
                 request_status=requests.get(s.id, "none"))
        for s in rows
    ]


@router.get("/mine", response_model=List[SpaceSummary])
@limiter.limit("60/minute")
async def my_spaces(db: db_dependency, org: org_dependency, request: Request):
    """Spaces this account belongs to.

    Resolved from SpaceMember and nothing else — not from the caller's org, and
    not from which org owns the space. That is the whole design in one query.
    """
    try:
        ids = spaces.space_ids_for(db, org.account_id)
        rows = (db.query(Space).filter(Space.id.in_(ids or [0]))
                  .order_by(Space.name.asc()).all())
        return _summaries(db, org, rows)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[MY SPACES]")


@router.get("/browse", response_model=List[SpaceSummary])
@limiter.limit("60/minute")
async def browse_spaces(db: db_dependency, org: org_dependency, request: Request,
                        q: str | None = None):
    """The public directory: every discoverable, active space.

    Deliberately has NO counterpart for organizations. A space holds shared
    content and is meant to be found; an org holds its members' private records,
    and a directory of joinable tenants is a different and far more dangerous
    object. Joining an org stays invite-only, by design.
    """
    try:
        query = db.query(Space).filter(Space.discoverable.is_(True),
                                       Space.status == SPACE_ACTIVE)
        if q:
            like = f"%{q.strip()}%"
            query = query.filter(Space.name.ilike(like))
        rows = query.order_by(Space.created_at.desc()).limit(100).all()
        return _summaries(db, org, rows)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[BROWSE SPACES]")


@router.post("/", status_code=status.HTTP_201_CREATED,
             response_model=SpaceDetail)
@limiter.limit("20/minute")
async def create_space(body: CreateSpaceRequest, db: db_dependency,
                       org: org_dependency, request: Request):
    """Create a space, owned by the caller and billed to their active org."""
    try:
        problem = spaces.slug_problem(db, body.slug)
        if problem:
            raise HTTPException(status_code=problem[0], detail=problem[1])

        space = spaces.create_space(
            db, slug=body.slug, name=body.name.strip(),
            description=body.description,
            owner_account_id=org.account_id,
            # Attribution, never tenancy: who is accountable and who is billed.
            # Nothing reads this to decide whether content may be opened.
            owner_org_id=org.org_id,
            discoverable=body.discoverable, join_policy=body.join_policy,
        )
        db.commit()
        db.refresh(space)
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[CREATE SPACE]")


def _detail(db, org, space: Space) -> SpaceDetail:
    member = spaces.membership(db, space.id, org.account_id)
    owner = bool(member and member.is_owner)
    count = _member_counts(db, [space.id]).get(space.id, 0)
    requests = _request_statuses(db, org.account_id, [space.id])

    tiers = (db.query(SpaceTier).filter(SpaceTier.space_id == space.id)
               .order_by(SpaceTier.id.asc()).all())

    members: List[SpaceMemberResponse] = []
    join_requests: List[JoinRequestResponse] = []
    if owner:
        # Owner-only. Who else is in a space is the owner's business — a member
        # enumerating the roster would be reading the other members' presence
        # in somebody's paid community.
        rows = (db.query(SpaceMember, Account)
                  .join(Account, Account.id == SpaceMember.account_id)
                  .filter(SpaceMember.space_id == space.id)
                  .order_by(SpaceMember.is_owner.desc(), Account.email.asc())
                  .all())
        members = [
            SpaceMemberResponse(
                account_id=m.account_id, email=a.email, tier_key=m.tier_key,
                is_owner=m.is_owner, granted_by=m.granted_by,
                created_at=m.created_at)
            for m, a in rows
        ]
        pending = (db.query(SpaceJoinRequest, Account)
                     .join(Account, Account.id == SpaceJoinRequest.account_id)
                     .filter(SpaceJoinRequest.space_id == space.id,
                             SpaceJoinRequest.status == REQUEST_PENDING)
                     .order_by(SpaceJoinRequest.created_at.asc()).all())
        join_requests = [
            JoinRequestResponse(
                id=r.id, account_id=r.account_id, email=a.email,
                status=r.status, message=r.message, created_at=r.created_at)
            for r, a in pending
        ]

    base = _summary(space, member=member, member_count=count,
                    request_status=requests.get(space.id, "none"))
    return SpaceDetail(
        **base.model_dump(),
        tiers=[SpaceTierResponse(tier_key=t.tier_key, label=t.label,
                                 description=t.description,
                                 purchasable=bool(t.stripe_price_id))
               for t in tiers],
        members=members, join_requests=join_requests,
    )


@router.get("/{space_id}", response_model=SpaceDetail)
@limiter.limit("120/minute")
async def get_space(space_id: int, db: db_dependency, org: org_dependency,
                    request: Request):
    """One space. Members see it; so does anybody, if it is discoverable.

    A private space the caller is not in returns the same 404 a nonexistent one
    does, so the endpoint cannot be used to confirm that a space exists.
    """
    try:
        space = spaces.visible_space(db, space_id, org.account_id)
        if space is None:
            raise NOT_FOUND
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[GET SPACE]")


@router.put("/{space_id}", response_model=SpaceDetail)
@limiter.limit("30/minute")
async def update_space(space_id: int, body: UpdateSpaceRequest,
                       db: db_dependency, org: org_dependency, request: Request):
    """Owners only. The slug is not editable — it is the public identity."""
    try:
        space = _owned_space(db, space_id, org.account_id)

        if body.name is not None:
            space.name = body.name.strip()
        if body.description is not None:
            space.description = body.description
        if body.discoverable is not None:
            space.discoverable = body.discoverable
        if body.join_policy is not None:
            space.join_policy = body.join_policy

        db.commit()
        db.refresh(space)
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[UPDATE SPACE]")


def _owned_space(db, space_id: int, account_id: int) -> Space:
    """The space, if this account owns it. 404 otherwise.

    Ownership is read from the MEMBERSHIP row, never from Space.owner_account_id
    — that column is attribution and survives removal, so trusting it would let
    a former owner keep administering a space they are no longer in.
    """
    space = db.query(Space).filter(Space.id == space_id).first()
    if space is None or not spaces.is_owner(db, space_id, account_id):
        raise NOT_FOUND
    return space


# ---------------------------------------------------------------------------
# getting in
# ---------------------------------------------------------------------------

@router.post("/{space_id}/join", status_code=status.HTTP_201_CREATED,
             response_model=SpaceDetail)
@limiter.limit("20/minute")
async def join_space(space_id: int, body: JoinRequestBody, db: db_dependency,
                     org: org_dependency, request: Request):
    """Join an open space, or ask to join a request-only one.

    One endpoint for both because the caller's intent is identical — "let me
    in" — and which of the two happens is the SPACE's policy, not something a
    client should be able to choose by picking a different URL.
    """
    try:
        space = spaces.visible_space(db, space_id, org.account_id)
        if space is None or space.status != SPACE_ACTIVE:
            raise NOT_FOUND
        if spaces.is_member(db, space.id, org.account_id):
            return _detail(db, org, space)
        if space.join_policy == JOIN_INVITE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This space is invite only.")

        if space.join_policy == JOIN_OPEN:
            spaces.add_member(db, space=space, account_id=org.account_id,
                              tier_key=TIER_MEMBER,
                              granted_by=GRANTED_BY_REQUEST,
                              actor_account_id=org.account_id)
            db.commit()
            return _detail(db, org, space)

        # request-only: one row per (space, account), reused if they were
        # turned down before — so a denied applicant cannot flood the queue and
        # the owner can still see they have asked before.
        existing = (db.query(SpaceJoinRequest)
                      .filter(SpaceJoinRequest.space_id == space.id,
                              SpaceJoinRequest.account_id == org.account_id)
                      .first())
        if existing and existing.status == REQUEST_PENDING:
            return _detail(db, org, space)

        if existing:
            existing.status = REQUEST_PENDING
            existing.message = body.message
            existing.decided_by_account_id = None
            existing.decided_at = None
        else:
            db.add(SpaceJoinRequest(space_id=space.id,
                                    account_id=org.account_id,
                                    status=REQUEST_PENDING,
                                    message=body.message))
        audit.record_space(db, event_type=audit.SPACE_REQUEST, space_id=space.id,
                           space_name=space.name, account_id=org.account_id,
                           actor_account_id=org.account_id)
        db.commit()
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[JOIN SPACE]")


@router.post("/{space_id}/members", status_code=status.HTTP_201_CREATED,
             response_model=SpaceDetail)
@limiter.limit("30/minute")
async def invite_member(space_id: int, body: InviteToSpaceRequest,
                        db: db_dependency, org: org_dependency,
                        request: Request):
    """Let somebody in for free, on a chosen tier.

    The other half of the paywall, and deliberately the same table: an invited
    member differs from a paying one only by `granted_by`. There is no separate
    "comp" concept to keep in step.

    Only an account that already exists can be invited. Creating one here would
    be creating an account on an unverified address at the request of a
    stranger's space — the org invite path can do that because an org is a
    workplace; a space is not.
    """
    try:
        space = _owned_space(db, space_id, org.account_id)

        account = (db.query(Account)
                     .filter(func.lower(Account.email) == body.email).first())
        if account is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No account with that email. They need to sign up first.")

        tier = (db.query(SpaceTier)
                  .filter(SpaceTier.space_id == space.id,
                          SpaceTier.tier_key == body.tier_key).first())
        if tier is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="That tier does not exist in this space.")

        spaces.add_member(db, space=space, account_id=account.id,
                          tier_key=body.tier_key, granted_by=GRANTED_BY_GRANT,
                          actor_account_id=org.account_id)
        db.commit()
        db.refresh(space)
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[INVITE TO SPACE]")


@router.post("/{space_id}/requests/{request_id}/{decision}",
             response_model=SpaceDetail)
@limiter.limit("60/minute")
async def decide_request(space_id: int, request_id: int, decision: str,
                         db: db_dependency, org: org_dependency,
                         request: Request):
    """Approve or deny somebody who asked to join. Owners only."""
    try:
        if decision not in ("approve", "deny"):
            raise NOT_FOUND
        space = _owned_space(db, space_id, org.account_id)

        join_request = (db.query(SpaceJoinRequest)
                          .filter(SpaceJoinRequest.id == request_id,
                                  SpaceJoinRequest.space_id == space.id)
                          .first())
        if join_request is None:
            raise NOT_FOUND

        join_request.decided_by_account_id = org.account_id
        join_request.decided_at = datetime.now(timezone.utc)

        if decision == "approve":
            join_request.status = REQUEST_APPROVED
            spaces.add_member(db, space=space,
                              account_id=join_request.account_id,
                              tier_key=TIER_MEMBER,
                              granted_by=GRANTED_BY_REQUEST,
                              actor_account_id=org.account_id)
            event = audit.SPACE_REQUEST_APPROVE
        else:
            join_request.status = REQUEST_DENIED
            event = audit.SPACE_REQUEST_DENY

        audit.record_space(db, event_type=event, space_id=space.id,
                           space_name=space.name,
                           account_id=join_request.account_id,
                           actor_account_id=org.account_id)
        db.commit()
        db.refresh(space)
        return _detail(db, org, space)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[DECIDE SPACE REQUEST]")


@router.delete("/{space_id}/members/{account_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def remove_member(space_id: int, account_id: int, db: db_dependency,
                        org: org_dependency, request: Request):
    """Remove somebody, or leave yourself.

    Both, because they are the same row and the same consequence. The one thing
    refused is removing the last owner: a space nobody can administer cannot be
    repaired from inside the product.
    """
    try:
        space = db.query(Space).filter(Space.id == space_id).first()
        if space is None:
            raise NOT_FOUND

        leaving = account_id == org.account_id
        if not leaving and not spaces.is_owner(db, space.id, org.account_id):
            raise NOT_FOUND

        member = spaces.membership(db, space.id, account_id)
        if member is None:
            raise NOT_FOUND
        if member.is_owner and spaces.owner_count(db, space.id) <= 1:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A space needs an owner. Make somebody else an owner "
                       "first.")

        spaces.remove_member(db, space=space, account_id=account_id,
                             actor_account_id=org.account_id)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[REMOVE SPACE MEMBER]")
