"""Tariff measure tools: look a measure up, and edit one only after approval.

  - tariffMeasureSearch: read-only, scoped to the agent's org;
  - tariffMeasureUpdate: checks the edit and queues it, never writes;
  - the executor (routers/tool_approvals/tariff_measure_update.py): re-checks
    access and that the measure is unchanged, then applies the edit.
"""
import datetime as dt
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.agent_runtime.tool_entitlement import TOOL_MODULES, allowed_tool_configs
from src.agent_runtime.tools.hitl_email_base import HITL_SENTINEL_KEY
from src.agent_runtime.tools.registry import ToolRegistry
from src.agent_runtime.tools.tariff_measures import (
    create_tariff_measure_search_tool,
    create_tariff_measure_update_tool,
)
from src.db.models import Organization
from src.db.tnd_models import TndMeasure
from src.routers.tool_approvals import tariff_measure_update as exec_mod
from src.routers.tool_approvals.tariff_measure_update import execute_tariff_measure_update
from src.schemas import validate_against_schema
from src.services import tnd_measure_edits as edits

D = dt.date


def _measure(db, org, *, program="section_301", hts="85443000", origin="CN", rate="0.025",
             frm=D(2026, 1, 1), to=None, status="proposed", **kw):
    m = TndMeasure(org_id=org.id, program=program, hts_code=hts, origin_country=origin,
                   rate_type=kw.pop("rate_type", "ad_valorem"),
                   ad_valorem_rate=Decimal(rate) if rate is not None else None,
                   effective_from=frm, effective_to=to, status=status,
                   description=kw.pop("description", f"{program} {rate}"), **kw)
    db.add(m)
    db.flush()
    return m


def _kwargs(db, org, session_factory=None):
    return dict(org_scope=SimpleNamespace(org_id=org.id, account_id=1), agent_id=None,
                chat_session_id_pk=None, session_factory=session_factory or (lambda: _NoClose(db)))


class _NoClose:
    """The test session, with close() a no-op so the tool can't end the test's transaction."""
    def __init__(self, db):
        self._db = db

    def __getattr__(self, name):
        return getattr(self._db, name)

    def close(self):
        pass


def _other_org(db) -> Organization:
    org = Organization(name="Other Co")
    db.add(org)
    db.flush()
    return org


# ── Registration / entitlement / schema ─────────────────────────────────────

@pytest.mark.parametrize("tool_type", ["tariffMeasureSearch", "tariffMeasureUpdate"])
def test_registered_and_gated_with_tariffs(tool_type):
    cfg = {"type": tool_type}
    assert callable(ToolRegistry.get_builder(tool_type))
    assert TOOL_MODULES[tool_type] == "tariffs"
    assert allowed_tool_configs([cfg], granted={"knowledge_bases"}) == []
    assert allowed_tool_configs([cfg], granted={"tariffs"}) == [cfg]
    validate_against_schema({"schema": "agent_config", "version": 4,
                             "data": {"systemPrompt": "x", "tools": [cfg]}}, "agent_config", 4)


async def test_no_org_no_tool():
    with pytest.raises(ValueError):
        await create_tariff_measure_update_tool(tool_config={}, account_id=1, db=MagicMock())


# ── Search ──────────────────────────────────────────────────────────────────

async def test_search_finds_code_parents_and_children_in_this_org_only(db: Session, test_org):
    exact = _measure(db, test_org)
    parent = _measure(db, test_org, hts="8544", program="section_232")
    _measure(db, test_org, hts="8703")                                 # unrelated code
    _measure(db, _other_org(db), hts="85443000")                       # another org
    tool = await create_tariff_measure_search_tool({}, 1, db, **_kwargs(db, test_org))

    out = await tool.coroutine(hts_code="8544.30.00")
    assert {m["measure_id"] for m in out["measures"]} == {exact.id, parent.id}
    first = next(m for m in out["measures"] if m["measure_id"] == exact.id)
    assert first["rate"] == "2.5%" and first["ad_valorem_rate"] == "0.0250"

    out = await tool.coroutine(hts_code="8544", program="section_301")
    assert [m["measure_id"] for m in out["measures"]] == [exact.id]


async def test_search_filters_by_status_and_date(db: Session, test_org):
    old = _measure(db, test_org, status="approved", to=D(2026, 6, 1))
    new = _measure(db, test_org, status="approved", frm=D(2026, 6, 1), rate="0.25")
    _measure(db, test_org, status="rejected")
    tool = await create_tariff_measure_search_tool({}, 1, db, **_kwargs(db, test_org))
    out = await tool.coroutine(status="approved", in_force_on=D(2026, 7, 1))
    assert [m["measure_id"] for m in out["measures"]] == [new.id]
    out = await tool.coroutine(status="approved", in_force_on=D(2026, 5, 31))
    assert [m["measure_id"] for m in out["measures"]] == [old.id]


# ── Update: the tool queues, never writes ───────────────────────────────────

async def _update_tool(db, org, queued):
    session = MagicMock()
    session.add.side_effect = queued.append
    # Reads go to the test DB; the approval row goes to the mock (queue_tool_approval
    # uses the same factory, so route by what is asked of it).
    def factory():
        return _Router(db, session)
    return await create_tariff_measure_update_tool({}, 7, db, **_kwargs(db, org, factory))


class _Router(_NoClose):
    def __init__(self, db, writes):
        super().__init__(db)
        self._writes = writes

    def add(self, obj):
        self._writes.add(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        obj.id = 555

    def rollback(self):
        pass


async def test_update_is_queued_with_before_and_after(db: Session, test_org):
    m = _measure(db, test_org)
    queued = []
    tool = await _update_tool(db, test_org, queued)
    result = json.loads(await tool.coroutine(
        measure_id=m.id, reason="Notice says 25%", ad_valorem_rate=0.25, clear_fields=["origin_country"]))

    assert result[HITL_SENTINEL_KEY] is True and result["tool_type"] == "tariffMeasureUpdate"
    assert result["preview"]["changes"] == [
        {"field": "ad_valorem_rate", "before": "0.0250", "after": "0.2500"},
        {"field": "origin_country", "before": "CN", "after": None},
    ]
    [approval] = queued
    assert approval.account_id == 7
    assert approval.payload == {"org_id": test_org.id, "measure_id": m.id,
                                "changes": {"ad_valorem_rate": "0.2500", "origin_country": None},
                                "before": {"ad_valorem_rate": "0.0250", "origin_country": "CN"},
                                "reason": "Notice says 25%"}
    db.refresh(m)
    assert m.ad_valorem_rate == Decimal("0.0250") and m.origin_country == "CN", "nothing written"


@pytest.mark.parametrize("args, error", [
    ({"ad_valorem_rate": 25}, "fraction"),
    ({"ad_valorem_rate": 0.025}, "nothing to change"),
    ({"effective_to": D(2025, 1, 1)}, "after effective_from"),
    ({"rate_type": "specific"}, "specific_rate and specific_unit"),
    ({"hts_code": "8"}, "2 to 10 digits"),
    ({"origin_country": "China"}, "two-letter"),
    ({}, "at least one field"),
])
async def test_invalid_edits_are_errors_not_approvals(db: Session, test_org, args, error):
    m = _measure(db, test_org)
    queued = []
    tool = await _update_tool(db, test_org, queued)
    result = json.loads(await tool.coroutine(measure_id=m.id, reason="r", **args))
    assert error in result["error"] and not queued


async def test_another_orgs_measure_cannot_be_targeted(db: Session, test_org):
    theirs = _measure(db, _other_org(db))
    queued = []
    tool = await _update_tool(db, test_org, queued)
    result = json.loads(await tool.coroutine(measure_id=theirs.id, reason="r", ad_valorem_rate=0.1))
    assert "No measure" in result["error"] and not queued


# ── The executor ────────────────────────────────────────────────────────────

def _approval(m, org, changes, before, reason="Notice says 25%"):
    return SimpleNamespace(id=9, status="pending", payload={
        "org_id": org.id, "measure_id": m.id, "changes": changes, "before": before, "reason": reason})


def _plan(m, **raw):
    return edits.plan_edit(m, raw)


def test_approval_applies_the_edit_and_keeps_status(db: Session, test_org, test_account):
    m = _measure(db, test_org)
    approval = _approval(m, test_org, *_plan(m, ad_valorem_rate=0.25, effective_to="2027-01-01"))
    message = execute_tariff_measure_update(db, approval, account_id=test_account.id)

    db.refresh(m)
    assert m.ad_valorem_rate == Decimal("0.2500") and m.effective_to == D(2027, 1, 1)
    assert m.status == "proposed" and m.reviewed_by_account_id is None, \
        "a proposed measure still needs its review on the Tariff Updates page"
    assert approval.status == "approved" and f"Measure {m.id} updated" in message


def test_editing_an_approved_rate_records_who_signed_off(db: Session, test_org, test_account):
    m = _measure(db, test_org, status="approved")
    approval = _approval(m, test_org, *_plan(m, ad_valorem_rate=0.25))
    execute_tariff_measure_update(db, approval, account_id=test_account.id)
    db.refresh(m)
    assert m.status == "approved" and m.reviewed_by_account_id == test_account.id
    assert m.reviewed_at is not None and m.review_note == "Edited in chat: Notice says 25%"


def test_a_measure_changed_since_is_not_overwritten(db: Session, test_org, test_account):
    m = _measure(db, test_org)
    approval = _approval(m, test_org, *_plan(m, ad_valorem_rate=0.25))
    m.ad_valorem_rate = Decimal("0.10")                       # somebody else's edit
    db.flush()
    with pytest.raises(HTTPException) as exc:
        execute_tariff_measure_update(db, approval, account_id=test_account.id)
    assert exc.value.status_code == 409 and approval.status == "pending"
    db.refresh(m)
    assert m.ad_valorem_rate == Decimal("0.1000")


def test_lost_tariffs_access_blocks_at_approval_time(monkeypatch, db: Session, test_org, test_account):
    m = _measure(db, test_org)
    approval = _approval(m, test_org, *_plan(m, ad_valorem_rate=0.25))
    monkeypatch.setattr(exec_mod, "effective_modules", lambda *a: {"knowledge_bases"})
    with pytest.raises(HTTPException) as exc:
        execute_tariff_measure_update(db, approval, account_id=test_account.id)
    assert exc.value.status_code == 403 and approval.status == "pending"


def test_non_member_cannot_approve_into_another_org(db: Session, test_org, test_account):
    other = _other_org(db)
    m = _measure(db, other)
    approval = _approval(m, other, *_plan(m, ad_valorem_rate=0.25))
    with pytest.raises(HTTPException) as exc:
        execute_tariff_measure_update(db, approval, account_id=test_account.id)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("payload", [
    {"changes": {}},
    {"before": {}},
    {"measure_id": "1"},
])
def test_bad_payload_is_a_422(db: Session, test_org, test_account, payload):
    m = _measure(db, test_org)
    approval = _approval(m, test_org, *_plan(m, ad_valorem_rate=0.25))
    approval.payload.update(payload)
    with pytest.raises(HTTPException) as exc:
        execute_tariff_measure_update(db, approval, account_id=test_account.id)
    assert exc.value.status_code == 422


# ── Through the approve endpoint ────────────────────────────────────────────

async def test_approve_endpoint_dispatches_tariff_edits(authed_client, db: Session, test_org, test_account):
    from src.db.models import PendingToolApproval

    m = _measure(db, test_org, status="approved")
    changes, before = _plan(m, ad_valorem_rate=0.25)
    row = PendingToolApproval(
        account_id=test_account.id, tool_type="tariffMeasureUpdate", status="pending",
        payload={"org_id": test_org.id, "measure_id": m.id, "changes": changes,
                 "before": before, "reason": "r"},
        expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5))
    db.add(row)
    db.flush()

    resp = await authed_client.post(f"/api/tool-approvals/{row.id}/approve")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "approved"
    db.refresh(m)
    assert m.ad_valorem_rate == Decimal("0.2500")
