"""The approval lookup the approve / reject / preview endpoints repeated."""
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.db.models import PendingToolApproval


def pending_approval_or_error(
    db: Session,
    approval_id: int,
    account_id: int,
    *,
    action: str,
    expired_detail: str = "This approval request has expired",
    mark_expired: bool = True,
) -> PendingToolApproval:
    """The caller's still-pending, unexpired approval, or the matching error.

    404 if the caller has no such approval, 409 if it is no longer pending
    (``action`` names the attempted verb in the message), 410 if it has passed
    its TTL. With ``mark_expired`` the expired row is also flipped to
    ``expired`` and committed before the 410 is raised.
    """
    now = datetime.now(timezone.utc)

    approval = db.query(PendingToolApproval).filter(
        PendingToolApproval.id == approval_id,
        PendingToolApproval.account_id == account_id,
    ).first()

    if not approval:
        raise HTTPException(status_code=404, detail="Tool approval request not found")

    if approval.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"Cannot {action} a request with status '{approval.status}'",
        )

    if approval.expires_at < now:
        if mark_expired:
            approval.status = "expired"
            db.commit()
        raise HTTPException(status_code=410, detail=expired_detail)

    return approval
