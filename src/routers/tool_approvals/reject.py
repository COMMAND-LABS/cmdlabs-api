from fastapi import APIRouter, Request
from src.deps import db_dependency, auth_dependency, account_id_from_claims
from .models import RejectToolApprovalResponse
from ._shared import pending_approval_or_error
from src.rate_limit import limiter

router = APIRouter()

@router.post("/{approval_id}/reject", response_model=RejectToolApprovalResponse)
@limiter.limit("60/minute")
async def reject_tool_approval(
    approval_id: int,
    db: db_dependency,
    auth: auth_dependency,
    request: Request,
):
    """Reject a pending tool action — no execution occurs."""
    account_id = account_id_from_claims(auth)
    approval = pending_approval_or_error(
        db, approval_id, account_id,
        action="reject",
        expired_detail="This approval request has already expired",
    )

    approval.status = "rejected"
    db.commit()

    return RejectToolApprovalResponse(
        id=approval.id,
        status="rejected",
        message="Tool action rejected",
    )
