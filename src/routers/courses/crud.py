"""
Which courses an organization has, and which of them a caller may open.

THE CONTENT IS NOT HERE
-----------------------
A course is a dynamic experience living in the Next.js router. This service
stores an ENABLEMENT — a stable `course_key` an org may reach, with a local
title and ordering — and answers one question the UI cannot answer for itself:
may THIS caller open THAT course, right now.

That answer has to come from the server. The dashboard's own canAccessPath()
runs in a client effect and redirects, which means the page renders and streams
first — fine for hiding a menu item, useless for gating paid courseware, since
the lesson paints before it vanishes. So `GET /api/courses/{course_key}` is the
gate a course's server component calls before rendering anything.

VISIBILITY
----------
    'org'     every member of the org (the common case: a client's whole team)
    'granted' only accounts or groups holding an AccessGrant (a department)
    'catalog' platform courseware, published once and visible to EVERY org,
              gated by required_plan instead of by membership

The first two ride on machinery that already exists and is already org-confined.
The third is one-directional by construction — only STAFF may mark a course
'catalog' (_assert_may_publish), so it can only ever add OUR content to a
tenant's view, never another tenant's rows.

It used to also require the row to live in the platform org. That second
condition existed because root was both the platform's content org and the
public signup lobby, so "belongs to the platform org" could not tell staff
content from a stranger's. Signups have had their own orgs since e3f4a5b6c7d8,
so the staff check alone now carries it — and dropping the org half is what let
the platform org stop being a special row.

PLANS
-----
`required_plan` ('free' | 'premium') decides which catalog courses a caller can
open. Listing is not opening: the browser shows locked premium courses to
somebody on the free plan, because seeing what you have not bought is the point
of a catalog, while GET /{course_key} still refuses them.
"""
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import and_, or_

from src.config import plans_registry as plans
from src.db.models import Course
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import access, spaces
from src.services.org_scope import tenant_predicate
from src.utils.errors import handle_db_error

logger = logging.getLogger(__name__)

router = APIRouter()

# Stable identifier matching a UI route. Shape is validated here; WHICH keys
# exist is owned by the UI (src/config/courses.ts), because that is where the
# content lives. Validating shape catches typos without forcing a second
# registry that would drift from the routes it describes.
COURSE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


class CourseResponse(BaseModel):
    id: int
    course_key: str
    title: str
    description: Optional[str] = None
    sort_order: int
    visibility: str
    # True when the caller reaches it through the whole-org arm rather than a
    # grant. Lets the owner's UI show "everyone" vs "specific people" without a
    # second call.
    org_wide: bool
    # Which plan opens it: 'free' | 'premium'. What the browser groups by.
    required_plan: str
    # True for platform courseware published to every org, false for a course
    # this org enabled for itself.
    catalog: bool
    # Set when the course lives in a SPACE rather than in an org. Its members
    # open it whatever org they belong to; None means it is an org enablement.
    space_id: Optional[int] = None
    # True when it is listed for BROWSING but this caller's plan cannot open
    # it. Only ever true for a catalog course — a tenant's own courses are
    # never listed locked, because which courses another team bought is not
    # something to advertise. GET /{course_key} 404s anything locked.
    locked: bool


VISIBILITIES = ("org", "granted", "catalog")


class CreateCourseRequest(BaseModel):
    course_key: str
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    sort_order: int = 0
    visibility: str = "org"
    required_plan: str = plans.PLAN_FREE
    # Put the course in a SPACE instead of in the caller's org. Requires
    # owning that space. Exactly one home — the database enforces it too
    # (ck_courses_one_home), so a bug here is a 500 rather than a leak.
    space_id: Optional[int] = None

    @field_validator("course_key")
    @classmethod
    def _key_shape(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not COURSE_KEY_PATTERN.match(v):
            raise ValueError(
                "Use 2-63 characters: lowercase letters, numbers and hyphens.")
        return v

    @field_validator("visibility")
    @classmethod
    def _known_visibility(cls, v: str) -> str:
        if v not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")
        return v

    @field_validator("required_plan")
    @classmethod
    def _known_plan(cls, v: str) -> str:
        if not plans.is_valid(v):
            raise ValueError(f"required_plan must be one of {plans.PLAN_KEYS}")
        return v


class UpdateCourseRequest(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    sort_order: Optional[int] = None
    visibility: Optional[str] = None
    required_plan: Optional[str] = None

    @field_validator("visibility")
    @classmethod
    def _known_visibility(cls, v):
        if v is not None and v not in VISIBILITIES:
            raise ValueError(f"visibility must be one of {VISIBILITIES}")
        return v

    @field_validator("required_plan")
    @classmethod
    def _known_plan(cls, v):
        if v is not None and not plans.is_valid(v):
            raise ValueError(f"required_plan must be one of {plans.PLAN_KEYS}")
        return v


def _require_owner(org):
    """Only an owner decides which courses their org has."""
    if not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")


def _assert_may_publish(db, org, visibility: Optional[str]) -> None:
    """Only platform STAFF may publish a course to every org.

    One condition now, where there used to be two. The second was "and it must
    live in the platform org", which existed because root held the public
    signups as well as the platform's content — so org membership alone could
    not tell staff content from a stranger's. Every account has owned its own
    org since e3f4a5b6c7d8, so `is_staff` carries the whole check.

    404 rather than 403: an org owner poking at `visibility: 'catalog'` learns
    nothing about whether such a thing exists.
    """
    if visibility != "catalog":
        return
    if not org.is_super_admin:
        logger.warning(
            "[COURSE] account %s (org %s) tried to publish a catalog course",
            org.account_id, org.org_id)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found")


def _writable_course(db, org, course_id: int) -> Course:
    """The course, if this caller may change it. 404 otherwise.

    Two homes, two owners, and they are not interchangeable: an ORG course
    belongs to the org's owner, a SPACE course to the space's owner. Being an
    org owner grants nothing over a space's content, and vice versa — which is
    the same wall the read path enforces, applied to writes.
    """
    course = db.query(Course).filter(Course.id == course_id).first()
    if course is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found")

    if course.space_id is not None:
        if not spaces.is_owner(db, course.space_id, org.account_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Not found")
        return course

    if course.org_id != org.org_id or not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Not found")
    return course


def _own_arm(db, org):
    """This org's own courses that the caller may open.

    Two additive sub-arms, both already confined to the caller's org by
    tenant_predicate — a grant can widen WHO inside the org, never WHICH org.
    """
    granted_ids = access.accessible_resource_ids(
        db, org.account_id, access.COURSE, required="read", org_id=org.org_id)

    arms = [Course.visibility == "org"]
    if granted_ids:
        arms.append(Course.id.in_(granted_ids))
    # An owner sees every course in their org regardless of grants — otherwise
    # they could enable a 'granted' course and then not be able to open it to
    # check what they just published.
    if org.is_owner:
        arms.append(Course.id.isnot(None))

    return and_(tenant_predicate(Course, org), or_(*arms))


def _space_arm(db, org):
    """Courses living in a space this caller belongs to.

    CONTAINER MEMBERSHIP IS THE GRANT. There is no visibility column consulted
    here and no per-course grant to check: being in the space is what opens its
    content, which is the entire point of a space and the reason it does not
    need an access mechanism of its own.

    Note what is NOT read — the space's owner_org_id. A space's content is
    reachable by its members, never by the org that happens to be billed for
    it. Asserted in tests/test_space_isolation.py.
    """
    space_ids = spaces.space_ids_for(db, org.account_id)
    if not space_ids:
        return None
    return Course.space_id.in_(space_ids)


def _catalog_arm(db):
    """Platform courseware, published once and visible to every org.

    Identified by the visibility alone. It used to also require the row to sit
    in the platform org; the write path is what makes that safe (only staff may
    set 'catalog'), and asking the read path to re-derive it through an org
    lookup added a query and a special row without adding a check.
    """
    return Course.visibility == "catalog"


def _query(db, org, *, browsing: bool):
    """Courses for this caller.

    `browsing=False` is THE GATE: only what they may actually open.
    `browsing=True` additionally lists catalog courses their plan does not
    cover, so somebody on the free plan can see what the paid one contains.

    The difference is deliberately confined to CATALOG rows. A tenant's own
    courses are never listed locked in either mode: that a course exists in
    another team is not something to advertise, and it was the reason the first
    version of this list rendered no locked cards at all.
    """
    arms = [_own_arm(db, org)]

    # Space courses are reached by membership alone — no plan gate, because the
    # space owner already decided who is in. A paid space charges at the door
    # (SpaceTier), not per course.
    space = _space_arm(db, org)
    if space is not None:
        arms.append(space)

    catalog = _catalog_arm(db)
    if catalog is not None:
        if not browsing and not plans.includes(org.plan, plans.PLAN_PREMIUM):
            # Gate mode: the plan has to cover it.
            catalog = and_(catalog, Course.required_plan == plans.PLAN_FREE)
        arms.append(catalog)

    return (db.query(Course).filter(or_(*arms))
              .order_by(Course.sort_order.asc(), Course.id.asc()))


def _visible(db, org):
    """Courses the caller may open. Used by the gate and by every writer."""
    return _query(db, org, browsing=False)


def _to_response(course: Course, plan: str) -> CourseResponse:
    is_catalog = course.visibility == "catalog"
    return CourseResponse(
        space_id=course.space_id,
        id=course.id, course_key=course.course_key, title=course.title,
        description=course.description, sort_order=course.sort_order,
        visibility=course.visibility, org_wide=(course.visibility == "org"),
        required_plan=course.required_plan,
        catalog=is_catalog,
        # Only a catalog row can be locked: everything else in this list is
        # already something the caller may open.
        locked=is_catalog and not plans.includes(plan, course.required_plan),
    )


@router.get("/", response_model=List[CourseResponse])
@limiter.limit("60/minute")
async def list_courses(db: db_dependency, org: org_dependency, request: Request):
    """The course browser: what this caller can open, plus what a plan away.

    Listing is not opening. Locked rows carry a title and a required_plan and
    nothing else worth having, and GET /{course_key} still refuses them — so
    the browser can show somebody on the free plan what premium contains
    without the list becoming a way to read it.
    """
    try:
        return [_to_response(c, org.plan)
                for c in _query(db, org, browsing=True).all()]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST COURSES]")


@router.get("/{course_key}", response_model=CourseResponse)
@limiter.limit("120/minute")
async def get_course(course_key: str, db: db_dependency, org: org_dependency,
                     request: Request):
    """THE GATE. A course's server component calls this before rendering.

    404 rather than 403 for a course the caller cannot open, and the same 404
    when the course does not exist here at all — so the response cannot be used
    to enumerate which courses another organization has bought.
    """
    try:
        course = _visible(db, org).filter(
            Course.course_key == course_key.strip().lower()).first()
        if not course:
            logger.info("[COURSE] account %s (org %s) denied %s",
                        org.account_id, org.org_id, course_key)
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Not found")
        return _to_response(course, org.plan)
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[GET COURSE]")


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=CourseResponse)
@limiter.limit("30/minute")
async def create_course(body: CreateCourseRequest, db: db_dependency,
                        org: org_dependency, request: Request):
    """Enable a course for this organization."""
    try:
        if body.space_id is not None:
            # A course in a space is the space owner's decision, not the org
            # owner's — so ownership is checked against the SPACE, and being an
            # org owner grants nothing here.
            if not spaces.is_owner(db, body.space_id, org.account_id):
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                    detail="Not found")
            if body.visibility == "catalog":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A space course is opened by the space's members. "
                           "It cannot also be catalog content.")
            existing = (db.query(Course)
                          .filter(Course.space_id == body.space_id,
                                  Course.course_key == body.course_key).first())
            if existing:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="That course is already in this space.")
            course = Course(
                org_id=None,
                space_id=body.space_id,
                course_key=body.course_key,
                title=body.title.strip(),
                description=body.description,
                sort_order=body.sort_order,
                # Membership in the space is the grant; there is no second
                # visibility rule and no plan gate on top of it.
                visibility="org",
                required_plan=plans.PLAN_FREE,
                account_id=org.account_id,
            )
            db.add(course)
            db.commit()
            db.refresh(course)
            return _to_response(course, org.plan)

        _require_owner(org)
        _assert_may_publish(db, org, body.visibility)

        existing = (db.query(Course)
                      .filter(Course.org_id == org.org_id,
                              Course.course_key == body.course_key).first())
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="That course is already enabled for this organization.")

        course = Course(
            org_id=org.org_id,
            course_key=body.course_key,
            title=body.title.strip(),
            description=body.description,
            sort_order=body.sort_order,
            visibility=body.visibility,
            required_plan=body.required_plan,
            # Attribution, never tenancy.
            account_id=org.account_id,
        )
        db.add(course)
        db.commit()
        db.refresh(course)
        return _to_response(course, org.plan)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[CREATE COURSE]")


@router.put("/{course_id}", response_model=CourseResponse)
@limiter.limit("30/minute")
async def update_course(course_id: int, body: UpdateCourseRequest,
                        db: db_dependency, org: org_dependency, request: Request):
    """Retitle or reorder. `course_key` is deliberately absent — it is the
    stable identifier grants are written against, so changing it would revoke
    access silently."""
    try:
        # Both directions: publishing a course INTO the catalog needs the
        # right, and so does editing one that is already there — otherwise the
        # check would only guard the create path.
        _assert_may_publish(db, org, body.visibility)
        course = _writable_course(db, org, course_id)
        _assert_may_publish(db, org, course.visibility)

        if body.title is not None:
            course.title = body.title.strip()
        if body.description is not None:
            course.description = body.description
        if body.sort_order is not None:
            course.sort_order = body.sort_order
        if body.visibility is not None:
            course.visibility = body.visibility
        if body.required_plan is not None:
            course.required_plan = body.required_plan

        db.commit()
        db.refresh(course)
        return _to_response(course, org.plan)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[UPDATE COURSE]")


@router.delete("/{course_id}", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def delete_course(course_id: int, db: db_dependency, org: org_dependency,
                        request: Request):
    """Disable a course for this org.

    Its grants go with it, logged individually — a revoke that leaves no trace
    is the one an audit cannot explain later.
    """
    try:
        course = _writable_course(db, org, course_id)

        from src.services.access_admin import revoke_resource_grants_logged
        revoke_resource_grants_logged(
            db, resource_type=access.COURSE, resource_id=course.id,
            actor_account_id=org.account_id)
        db.delete(course)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[DELETE COURSE]")
