"""
Remember which org the caller is acting in, so the next sign-in resumes there.

    PUT /{org_id}/active     MEMBER. Writes accounts.default_org_id.

WHY THIS EXISTS. The active org is a cookie (deps.ORG_COOKIE_NAME) that login
clears on purpose — a selection must not outlive the session that made it. But
nothing then remembered the selection at all: default_org_id was written only
when NULL, so it stayed pinned to the first org an account ever joined, and
everyone who worked in a second org was dropped back into the first one at
every sign-in. The UI's switch route now calls this alongside writing the
cookie, so "default" means "where you last were".

Membership is proven by named_org_dependency from the id in the path, the same
gate every other named-org route uses; a non-member gets 403 and nothing is
written. Not audited: where someone lands is a preference, not an access
change.
"""
from fastapi import APIRouter, Request
from pydantic import BaseModel

from src.db.models import Account
from src.deps import db_dependency, named_org_dependency
from src.rate_limit import limiter

router = APIRouter()


class ActiveOrgResponse(BaseModel):
    active_org_id: int


@router.put("/{org_id}/active", response_model=ActiveOrgResponse)
@limiter.limit("60/minute")
async def set_active_organization(db: db_dependency, org: named_org_dependency,
                                  request: Request):
    account = db.query(Account).filter(Account.id == org.account_id).one()
    if account.default_org_id != org.org_id:
        account.default_org_id = org.org_id
        db.commit()
    return ActiveOrgResponse(active_org_id=org.org_id)
