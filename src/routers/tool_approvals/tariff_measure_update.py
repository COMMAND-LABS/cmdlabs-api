"""Execute an approved tariffMeasureUpdate: write the edit to tnd_measures.

Checked again at approval time, not trusted from when the agent asked:
  - the approver may still open Tariffs in the measure's org (a role or plan
    can change in the 30 minutes an approval lives);
  - the measure still has the values the approver was shown as "before". If
    somebody changed it since, the edit is refused (409) rather than written
    over their change; the agent can propose it again against the new values;
  - the edited row still passes services/tnd_measure_edits.check_row.

The measure keeps its status. On an APPROVED measure the approver becomes its
latest reviewer (reviewed_by / reviewed_at / review_note): they signed off on
the rate calculations now use, so the row says who did and why. A proposed
measure still needs its review on the Tariff Updates page.

The approval row itself is the record of the edit: who approved it, when, and
the before / after values in its payload.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.agent_runtime.tool_entitlement import effective_modules
from src.db.models import PendingToolApproval
from src.db.tnd_models import TndMeasure
from src.services import tnd_measure_edits as edits

logger = logging.getLogger(__name__)


def execute_tariff_measure_update(db: Session, approval: PendingToolApproval, *,
                                  account_id: int) -> str:
    """Apply the edit. Marks the approval approved and returns the user-facing
    message; raises HTTPException and leaves it pending otherwise."""
    payload = approval.payload or {}
    org_id = payload.get("org_id")
    measure_id = payload.get("measure_id")
    changes = payload.get("changes")
    before = payload.get("before")
    reason = (payload.get("reason") or "").strip()

    if (not isinstance(org_id, int) or not isinstance(measure_id, int)
            or not isinstance(changes, dict) or not changes
            or not isinstance(before, dict) or set(before) != set(changes)):
        raise HTTPException(status_code=422, detail="Approval payload is incomplete.")

    if "tariffs" not in effective_modules(db, account_id, org_id):
        raise HTTPException(status_code=403,
                            detail="You no longer have access to Tariff Updates in this organization.")

    measure = (db.query(TndMeasure)
               .filter(TndMeasure.id == measure_id, TndMeasure.org_id == org_id)
               .with_for_update().first())
    if measure is None:
        raise HTTPException(status_code=404, detail="That tariff measure no longer exists.")

    if edits.snapshot(measure, before) != before:
        raise HTTPException(status_code=409, detail=(
            "This measure was changed after the agent proposed the edit. "
            "Reject this one and ask the agent again."))
    try:
        # Validates every value before setting any, so a refusal leaves the
        # row untouched.
        edits.apply_edit(measure, changes)
    except edits.MeasureEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if measure.status == "approved":
        measure.reviewed_by_account_id = account_id
        measure.reviewed_at = dt.datetime.now(dt.timezone.utc)
        measure.review_note = f"Edited in chat: {reason}" if reason else "Edited in chat"
    approval.status = "approved"
    db.commit()
    logger.info("tariffMeasureUpdate approval %s: measure %s updated (%s)",
                approval.id, measure_id, ", ".join(changes))

    fields = ", ".join(f.replace("_", " ") for f in changes)
    return f"Measure {measure_id} updated: {fields}."
