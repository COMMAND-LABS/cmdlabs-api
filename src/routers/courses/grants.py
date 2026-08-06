"""
Share a course with a group or an individual inside the organization.

Deliberately thin. Every rule that matters already lives in
services/access_admin.upsert_grant — same-org validation, the audit event, the
one chokepoint where AccessGrant rows are written — so this file resolves the
principal and gets out of the way. A second grant-writing path is how the
same-org check ends up enforced in one place and not the other.

Only reaches courses whose visibility is 'granted'. Granting an org-wide course
to a group would be a row that changes nothing, and a row that looks like
access without being it is worse than no row.
"""
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, model_validator

from src.db.models import AccessGrant, Course
from src.deps import db_dependency, org_dependency
from src.rate_limit import limiter
from src.services import access
from src.services.access_admin import (
    grant_label,
    record_access_event,
    resolve_principal,
    upsert_grant,
)
from src.services.org_scope import tenant_predicate
from src.utils.errors import handle_db_error

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateCourseGrantRequest(BaseModel):
    """Share with a group OR an individual. Exactly one."""
    accessGroupId: Optional[int] = None
    granteeEmail: Optional[str] = None

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.accessGroupId is None) == (self.granteeEmail is None):
            raise ValueError("Provide exactly one of accessGroupId or granteeEmail")
        return self


class CourseGrantResponse(BaseModel):
    id: int
    course_id: int
    label: str
    target_type: str          # 'group' | 'individual'


def _owned_course(db, org, course_id: int) -> Course:
    if not org.is_owner:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    course = (db.query(Course)
                .filter(Course.id == course_id, tenant_predicate(Course, org))
                .first())
    if not course:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return course


@router.get("/{course_id}/access-grants", response_model=List[CourseGrantResponse])
@limiter.limit("30/minute")
async def list_course_grants(course_id: int, db: db_dependency,
                             org: org_dependency, request: Request):
    try:
        course = _owned_course(db, org, course_id)
        grants = (db.query(AccessGrant)
                    .filter(AccessGrant.resource_type == access.COURSE,
                            AccessGrant.resource_id == course.id,
                            AccessGrant.org_id == org.org_id)
                    .all())
        return [
            CourseGrantResponse(
                id=g.id, course_id=course.id, label=grant_label(db, g),
                target_type=("group" if g.principal_type == access.GROUP
                             else "individual"),
            )
            for g in grants
        ]
    except HTTPException:
        raise
    except Exception as e:
        raise handle_db_error(e, "[LIST COURSE GRANTS]")


@router.post("/{course_id}/access-grants", status_code=status.HTTP_201_CREATED,
             response_model=CourseGrantResponse)
@limiter.limit("20/minute")
async def create_course_grant(course_id: int, body: CreateCourseGrantRequest,
                              db: db_dependency, org: org_dependency,
                              request: Request):
    try:
        course = _owned_course(db, org, course_id)

        if course.visibility != "granted":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This course is already open to everyone in the "
                       "organization. Set it to 'granted' first if you want to "
                       "narrow it.",
            )

        principal_type, principal_id, _label = resolve_principal(
            db,
            caller_account_id=org.account_id,
            access_group_id=body.accessGroupId,
            grantee_email=body.granteeEmail,
        )

        grant = upsert_grant(
            db,
            org_id=org.org_id,
            principal_type=principal_type,
            principal_id=principal_id,
            resource_type=access.COURSE,
            resource_id=course.id,
            role="read",
        )
        record_access_event(
            db,
            event_type="create",
            actor_account_id=org.account_id,
            resource_type=access.COURSE,
            resource_id=course.id,
            principal_type=principal_type,
            principal_id=principal_id,
            role="read",
        )
        db.commit()
        db.refresh(grant)
        return CourseGrantResponse(
            id=grant.id, course_id=course.id, label=grant_label(db, grant),
            target_type=("group" if principal_type == access.GROUP
                         else "individual"),
        )
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[CREATE COURSE GRANT]")


@router.delete("/{course_id}/access-grants/{grant_id}",
               status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def revoke_course_grant(course_id: int, grant_id: int, db: db_dependency,
                              org: org_dependency, request: Request):
    """Takes effect on the caller's next request — access resolves per request,
    so there is nothing cached to invalidate."""
    try:
        course = _owned_course(db, org, course_id)
        grant = (db.query(AccessGrant)
                   .filter(AccessGrant.id == grant_id,
                           AccessGrant.resource_type == access.COURSE,
                           AccessGrant.resource_id == course.id,
                           AccessGrant.org_id == org.org_id)
                   .first())
        if not grant:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="Not found")

        record_access_event(
            db,
            event_type="revoke",
            actor_account_id=org.account_id,
            resource_type=access.COURSE,
            resource_id=course.id,
            principal_type=grant.principal_type,
            principal_id=grant.principal_id,
            role=grant.role,
        )
        db.delete(grant)
        db.commit()
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise handle_db_error(e, "[REVOKE COURSE GRANT]")
