"""Tariff measure tools: find a measure, and propose an edit to one.

- tariffMeasureSearch (search_tariff_measures): read-only lookup in the org's
  tnd_measures, so the agent can find the measure a person is talking about
  and its id. Changes nothing, so it needs no approval.
- tariffMeasureUpdate (update_tariff_measure): proposes an edit. It never
  writes: it checks the edit (services/tnd_measure_edits.py) and queues it for
  the person in the chat, and routers/tool_approvals/tariff_measure_update.py
  applies it once they approve. A wrong rate is a wrong number finance acts
  on, which is the rule the whole tariffs module is built on (db/tnd_models.py).

Both act in the org the agent runs in (org_scope) and are gated on the
'tariffs' module of the person chatting (tool_entitlement.TOOL_MODULES): rates
are the org's shared data, not the agent owner's, so a shared agent does not
lend its owner's access to them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
from typing import Any, Literal

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
from sqlalchemy import or_

from src.db.tnd_models import TndMeasure
from src.services import tnd_measure_edits as edits
from src.services.tnd_rates import normalize_hts
from src.utils.errors import own_message_or_reason

from .hitl_email_base import queue_tool_approval
from .sessions import resolve_session_factory

logger = logging.getLogger(__name__)

SEARCH_TOOL_NAME = "search_tariff_measures"
UPDATE_TOOL_TYPE = "tariffMeasureUpdate"
UPDATE_TOOL_NAME = "update_tariff_measure"
MAX_RESULTS = 25


def _org_id(kwargs: dict, tool_type: str) -> int:
    org_scope = kwargs.get("org_scope")
    org_id = getattr(org_scope, "org_id", None)
    if org_id is None:
        # Skipped by the factory, like any misconfigured tool.
        raise ValueError(f"{tool_type}: no organization to act in")
    return org_id


def measure_summary(m: TndMeasure) -> dict[str, Any]:
    """What the model and the approval card show for one measure."""
    return {
        "measure_id": m.id,
        "status": m.status,
        "program": m.program,
        "hts_code": m.hts_code,
        "origin_country": m.origin_country,
        "rate": edits.describe_rate(m),
        **edits.snapshot(m, ("rate_type", "ad_valorem_rate", "specific_rate",
                             "specific_unit", "effective_from", "effective_to")),
        "description": (m.description or "")[:300],
    }


# ── Search ──────────────────────────────────────────────────────────────────

class SearchInput(BaseModel):
    hts_code: str | None = Field(
        default=None,
        description="Tariff code, with or without dots (e.g. 8544.30.00). Finds measures on "
                    "this code, its parent codes and its sub-codes.")
    origin_country: str | None = Field(
        default=None, description="Two-letter origin country code, e.g. CN.")
    program: str | None = Field(
        default=None, description="Duty program, e.g. mfn (base rate), section_301, section_232, ieepa.")
    status: Literal["proposed", "approved", "rejected"] | None = Field(
        default=None, description="Only measures with this review status. Omit for all.")
    in_force_on: dt.date | None = Field(
        default=None, description="Only measures in force on this date (YYYY-MM-DD).")
    limit: int = Field(default=10, ge=1, le=MAX_RESULTS)


def search_measures(db, org_id: int, *, hts_code=None, origin_country=None, program=None,
                    status=None, in_force_on=None, limit=10) -> list[TndMeasure]:
    q = db.query(TndMeasure).filter(TndMeasure.org_id == org_id)
    if hts_code:
        code = normalize_hts(hts_code)
        if len(code) < 2:
            raise ValueError("hts_code needs at least 2 digits")
        parents = [code[:n] for n in range(2, len(code))]
        q = q.filter(or_(TndMeasure.hts_code.startswith(code), TndMeasure.hts_code.in_(parents)))
    if origin_country:
        q = q.filter(TndMeasure.origin_country == origin_country.strip().upper())
    if program:
        q = q.filter(TndMeasure.program == program.strip().lower())
    if status:
        q = q.filter(TndMeasure.status == status)
    if in_force_on:
        q = q.filter(TndMeasure.effective_from <= in_force_on,
                     or_(TndMeasure.effective_to.is_(None), TndMeasure.effective_to > in_force_on))
    return (q.order_by(TndMeasure.effective_from.desc(), TndMeasure.id.desc())
            .limit(min(limit, MAX_RESULTS)).all())


async def create_tariff_measure_search_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    org_id = _org_id(kwargs, "tariffMeasureSearch")
    session_factory = resolve_session_factory(kwargs)

    def _search(**filters) -> dict:
        session = session_factory()
        try:
            rows = search_measures(session, org_id, **filters)
            return {"measures": [measure_summary(m) for m in rows], "count": len(rows)}
        except ValueError as exc:
            return {"error": own_message_or_reason(exc)}
        finally:
            session.close()

    async def _run(**filters) -> dict:
        return await asyncio.to_thread(_search, **filters)

    return StructuredTool.from_function(
        coroutine=_run,
        name=SEARCH_TOOL_NAME,
        description=tool_config.get("description") or (
            "Look up tariff measures (duty rates) your organization tracks, by tariff code, "
            "origin, program, review status or date. Returns each measure's id, rate, dates and "
            "status. Only approved measures count in duty calculations."
        ),
        args_schema=SearchInput,
    )


# ── Update (queued for approval) ────────────────────────────────────────────

class UpdateInput(BaseModel):
    measure_id: int = Field(description="The measure to change, from search_tariff_measures.")
    reason: str = Field(min_length=1, max_length=1000,
                        description="Why it should change, e.g. what the source says. Shown to the approver.")
    hts_code: str | None = Field(default=None, description="New tariff code (2 to 10 digits).")
    origin_country: str | None = Field(default=None, description="New two-letter origin country code.")
    program: str | None = Field(default=None, description="New program, e.g. section_301.")
    rate_type: Literal["ad_valorem", "specific", "compound"] | None = None
    ad_valorem_rate: float | None = Field(
        default=None, description="New percentage rate AS A FRACTION: 0.25 for 25%, 0.025 for 2.5%.")
    specific_rate: float | None = Field(default=None, description="New amount per unit, in dollars.")
    specific_unit: str | None = Field(default=None, description="Unit for specific_rate, e.g. kg.")
    effective_from: dt.date | None = Field(default=None, description="New start date (YYYY-MM-DD).")
    effective_to: dt.date | None = Field(
        default=None, description="New end date (YYYY-MM-DD); the measure stops applying on this day.")
    excluded_hts: list[str] | None = Field(
        default=None, description="Replaces the list of tariff codes excluded from the measure.")
    description: str | None = Field(default=None, description="New description of the measure.")
    clear_fields: list[Literal["hts_code", "origin_country", "ad_valorem_rate", "specific_rate",
                               "specific_unit", "effective_to"]] = Field(
        default_factory=list,
        description="Fields to empty: hts_code/origin_country = all codes/origins, "
                    "effective_to = no end date.")


def plan_update(db, org_id: int, measure_id: int, raw: dict[str, Any]) -> tuple[TndMeasure, dict, dict]:
    """(measure, changes, before) for an edit, or MeasureEditError."""
    measure = (db.query(TndMeasure)
               .filter(TndMeasure.id == measure_id, TndMeasure.org_id == org_id).first())
    if measure is None:
        raise edits.MeasureEditError(f"No measure {measure_id} in this organization.")
    changes, before = edits.plan_edit(measure, raw)
    return measure, changes, before


async def create_tariff_measure_update_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    org_id = _org_id(kwargs, UPDATE_TOOL_TYPE)
    agent_id: int | None = kwargs.get("agent_id")
    chat_session_id: int | None = kwargs.get("chat_session_id_pk")
    session_factory = resolve_session_factory(kwargs)

    def _plan(measure_id: int, raw: dict) -> tuple[dict, dict, dict]:
        session = session_factory()
        try:
            measure, changes, before = plan_update(session, org_id, measure_id, raw)
            return measure_summary(measure), changes, before
        finally:
            session.close()

    async def _update(measure_id: int, reason: str, clear_fields: list[str] | None = None,
                      **fields) -> str:
        raw = {f: v for f, v in fields.items() if v is not None}
        for f in clear_fields or []:
            if f in raw:
                return json.dumps({"error": f"'{f}' is both set and cleared; pick one."})
            raw[f] = None
        if not raw:
            return json.dumps({"error": "Say what to change: at least one field, or clear_fields."})
        try:
            summary, changes, before = await asyncio.to_thread(_plan, measure_id, raw)
        except edits.MeasureEditError as exc:
            return json.dumps({"error": str(exc)})

        return await queue_tool_approval(
            # The person in the chat approves: the queue is listed per account.
            account_id=account_id,
            agent_id=agent_id,
            chat_session_id=chat_session_id,
            session_factory=session_factory,
            tool_type=UPDATE_TOOL_TYPE,
            payload={"org_id": org_id, "measure_id": measure_id, "changes": changes,
                     "before": before, "reason": reason.strip()},
            preview={"measure": summary, "reason": reason.strip(),
                     "changes": [{"field": f, "before": before[f], "after": changes[f]}
                                 for f in changes]},
            message=(
                f"The change to measure {measure_id} is waiting for the user's approval. "
                "Nothing has changed yet. It keeps its review status ("
                f"{summary['status']}) once approved."
            ),
        )

    return StructuredTool.from_function(
        coroutine=_update,
        name=UPDATE_TOOL_NAME,
        description=tool_config.get("description") or (
            "Correct a tariff measure (duty rate) your organization tracks: its rate, dates, "
            "tariff code, origin or description. Find the measure_id with "
            "search_tariff_measures first. The change is shown to the user and only made once "
            "they approve it. It does not approve or reject measures; that happens on the "
            "Tariff Updates page."
        ),
        args_schema=UpdateInput,
    )

