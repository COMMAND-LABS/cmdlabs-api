"""
An edit to one tariff measure (db/tnd_models.TndMeasure), checked twice.

The agent's tariffMeasureUpdate tool proposes an edit and queues it for a
person (agent_runtime/tools/tariff_measures.py); on approval,
routers/tool_approvals/tariff_measure_update.py applies it. Both halves check
the edit with the code here, so what the approver was shown as valid is
exactly what gets written.

What an edit may touch is EDITABLE_FIELDS. Not status: approving or rejecting
a measure stays on the Tariff Updates page (routers/tariffs), and an edit keeps
the measure's status. Not the provenance columns either (source document,
quote, confidence, extracted_by): those record what the scraper read, and an
edit is a correction on top of it, not a rewrite of it.

Values travel through the approval payload as JSON, so every value has a
JSON form (Decimal -> str, date -> ISO string) and back.
"""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from src.db.tnd_models import RATE_TYPES, TndMeasure
from src.services.tnd_rates import normalize_hts

EDITABLE_FIELDS = (
    "hts_code", "origin_country", "program", "rate_type", "ad_valorem_rate",
    "specific_rate", "specific_unit", "effective_from", "effective_to",
    "excluded_hts", "description",
)
# Fields an edit may set to "none": all codes, all origins, no end date, ...
NULLABLE_FIELDS = frozenset({
    "hts_code", "origin_country", "ad_valorem_rate", "specific_rate",
    "specific_unit", "effective_to",
})

_PROGRAM = re.compile(r"^[a-z0-9_]{1,40}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_DECIMAL_FIELDS = ("ad_valorem_rate", "specific_rate")
_DATE_FIELDS = ("effective_from", "effective_to")
# An ad valorem rate is a fraction (0.25 = 25%). Some duties exceed 100%, so
# the ceiling is generous; it exists to catch "25" meant as 25%.
MAX_AD_VALOREM = Decimal("10")


class MeasureEditError(ValueError):
    """The edit is not valid; the message is written for the person/model."""


def _hts(value: Any, field: str) -> str:
    code = normalize_hts(str(value))
    if not 2 <= len(code) <= 10:
        raise MeasureEditError(f"{field}: a tariff code has 2 to 10 digits (got {value!r}).")
    return code


def _decimal(value: Any, field: str) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise MeasureEditError(f"{field}: {value!r} is not a number.") from None
    if not d.is_finite() or d < 0:
        raise MeasureEditError(f"{field}: must be zero or more.")
    if field == "ad_valorem_rate" and d > MAX_AD_VALOREM:
        raise MeasureEditError(
            f"ad_valorem_rate: {value} is too high. Give a fraction: 0.25 for 25%.")
    if field == "specific_rate" and d >= Decimal("1e10"):
        raise MeasureEditError("specific_rate: too large.")
    return d.quantize(Decimal("0.0001"))


def _date(value: Any, field: str) -> dt.date:
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value))
    except ValueError:
        raise MeasureEditError(f"{field}: {value!r} is not a date (YYYY-MM-DD).") from None


def parse_value(field: str, value: Any) -> Any:
    """One field's new value, normalized to what the column stores."""
    if field not in EDITABLE_FIELDS:
        raise MeasureEditError(f"'{field}' cannot be edited.")
    if value is None:
        if field not in NULLABLE_FIELDS:
            raise MeasureEditError(f"{field} cannot be empty.")
        return None
    if field == "hts_code":
        return _hts(value, field)
    if field == "origin_country":
        code = str(value).strip().upper()
        if not _COUNTRY.match(code):
            raise MeasureEditError("origin_country: a two-letter country code, e.g. CN.")
        return code
    if field == "program":
        program = str(value).strip().lower()
        if not _PROGRAM.match(program):
            raise MeasureEditError("program: lowercase letters, digits and '_', e.g. section_301.")
        return program
    if field == "rate_type":
        if value not in RATE_TYPES:
            raise MeasureEditError(f"rate_type: one of {', '.join(RATE_TYPES)}.")
        return value
    if field in _DECIMAL_FIELDS:
        return _decimal(value, field)
    if field == "specific_unit":
        unit = str(value).strip()
        if not 1 <= len(unit) <= 20:
            raise MeasureEditError("specific_unit: 1 to 20 characters, e.g. kg.")
        return unit
    if field in _DATE_FIELDS:
        return _date(value, field)
    if field == "excluded_hts":
        if not isinstance(value, list) or len(value) > 200:
            raise MeasureEditError("excluded_hts: a list of up to 200 tariff codes.")
        return sorted({_hts(v, field) for v in value})
    # description
    text = str(value).strip()
    if not 1 <= len(text) <= 4000:
        raise MeasureEditError("description: 1 to 4000 characters.")
    return text


def to_json(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _DECIMAL_FIELDS:
        return str(Decimal(value).quantize(Decimal("0.0001")))
    if field in _DATE_FIELDS:
        return value.isoformat()
    if field == "excluded_hts":
        return list(value)
    return value


def from_json(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _DECIMAL_FIELDS:
        return Decimal(value)
    if field in _DATE_FIELDS:
        return dt.date.fromisoformat(value)
    return value


def snapshot(measure: TndMeasure, fields) -> dict[str, Any]:
    """The JSON form of `fields` as the measure has them now."""
    return {f: to_json(f, getattr(measure, f)) for f in fields}


def check_row(state: dict[str, Any]) -> None:
    """Whole-row rules for a measure's values after an edit."""
    rate_type = state["rate_type"]
    if rate_type in ("ad_valorem", "compound") and state["ad_valorem_rate"] is None:
        raise MeasureEditError(f"A {rate_type} rate needs ad_valorem_rate.")
    if rate_type in ("specific", "compound") and (
            state["specific_rate"] is None or not state["specific_unit"]):
        raise MeasureEditError(f"A {rate_type} rate needs specific_rate and specific_unit.")
    if state["effective_to"] is not None and state["effective_to"] <= state["effective_from"]:
        raise MeasureEditError("effective_to must be after effective_from.")


def plan_edit(measure: TndMeasure, raw: dict[str, Any]) -> tuple[dict, dict]:
    """(changes, before), both JSON, for the fields `raw` actually changes.

    Raises MeasureEditError if a value is invalid, the row would break a rule,
    or nothing would change.
    """
    parsed = {f: parse_value(f, v) for f, v in raw.items()}
    changed = {f: v for f, v in parsed.items() if to_json(f, v) != to_json(f, getattr(measure, f))}
    if not changed:
        raise MeasureEditError("Those values are what the measure already has; nothing to change.")
    state = {f: getattr(measure, f) for f in EDITABLE_FIELDS} | changed
    check_row(state)
    return ({f: to_json(f, v) for f, v in changed.items()},
            snapshot(measure, changed))


def apply_edit(measure: TndMeasure, changes: dict[str, Any]) -> None:
    """Write planned `changes` (JSON form) onto the measure, re-checking them
    against the row as it is now."""
    parsed = {f: parse_value(f, from_json(f, v)) for f, v in changes.items()}
    check_row({f: getattr(measure, f) for f in EDITABLE_FIELDS} | parsed)
    for field, value in parsed.items():
        setattr(measure, field, value)


def describe_rate(measure: TndMeasure) -> str:
    """'25%', '$0.044/kg', '2.5% + $0.044/kg' — for people and the model."""
    parts = []
    if measure.ad_valorem_rate is not None:
        pct = (Decimal(measure.ad_valorem_rate) * 100).normalize()
        parts.append(f"{pct:f}%")
    if measure.specific_rate is not None:
        parts.append(f"${Decimal(measure.specific_rate).normalize():f}/{measure.specific_unit or 'unit'}")
    return " + ".join(parts) or "no rate"
