"""Tests for serialize_value — the DB-row -> JSON boundary for db tools.

Every value returned by a db read/write tool is json.dumps'd twice (once into
the LLM's tool result, once into the SSE frame the browser parses), so the
contract this file pins down is simple: whatever serialize_value returns must
survive json.dumps in strict mode.
"""

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum

import pytest

from src.agent_runtime.tools.db_read import serialize_value


def dumps_strict(value):
    """json.dumps that rejects NaN/Infinity, like the browser's JSON.parse."""
    return json.dumps(value, allow_nan=False)


# ── NUMERIC / Decimal ────────────────────────────────────────────────────────
# The regression: a dbTableRead on a table with a NUMERIC column (deals.amount)
# returned raw Decimals and the stream died with
# "Object of type Decimal is not JSON serializable".

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (Decimal("1500.00"), 1500.0),
        (Decimal("1500.10"), 1500.10),   # not exact in binary; repr round-trips
        (Decimal("0.1"), 0.1),
        (Decimal("-42.75"), -42.75),
        (Decimal("0"), 0.0),
    ],
)
def test_decimal_becomes_json_number(raw, expected):
    result = serialize_value(raw)
    assert result == expected
    assert isinstance(result, float)
    dumps_strict(result)


def test_decimal_beyond_float_precision_falls_back_to_string():
    """Better an exact string than a silently rounded number."""
    raw = Decimal("1.2345678901234567890123")
    assert serialize_value(raw) == "1.2345678901234567890123"


@pytest.mark.parametrize("raw", [Decimal("NaN"), Decimal("Infinity")])
def test_non_finite_decimal_is_stringified(raw):
    # NUMERIC accepts 'NaN'; emitting a bare NaN token would break the SSE
    # frame for every client that parses strictly.
    dumps_strict(serialize_value(raw))


def test_non_finite_float_is_stringified():
    dumps_strict(serialize_value(float("inf")))
    dumps_strict(serialize_value(float("nan")))


# ── Other driver types with no JSON equivalent ───────────────────────────────

def test_uuid_becomes_string():
    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert serialize_value(value) == "12345678-1234-5678-1234-567812345678"


def test_datetime_and_date_use_isoformat():
    moment = datetime(2026, 8, 2, 15, 30, tzinfo=timezone.utc)
    assert serialize_value(moment) == moment.isoformat()
    assert serialize_value(date(2026, 8, 2)) == "2026-08-02"


def test_interval_becomes_string():
    dumps_strict(serialize_value(timedelta(days=1, hours=2)))


def test_enum_unwraps_to_its_value():
    class Stage(Enum):
        LEAD = "lead"

    assert serialize_value(Stage.LEAD) == "lead"


def test_bytes_and_memoryview_decode():
    assert serialize_value(b"hello") == "hello"
    assert serialize_value(memoryview(b"hello")) == "hello"


# ── Containers ───────────────────────────────────────────────────────────────

def test_arrays_and_json_columns_are_serialized_elementwise():
    """A NUMERIC[] column or a JSONB blob hides Decimals one level down."""
    row = {
        "amounts": [Decimal("1.50"), Decimal("2.50")],
        "meta": {"total": Decimal("4.00"), "id": uuid.UUID(int=1)},
    }
    result = serialize_value(row)
    assert result == {
        "amounts": [1.5, 2.5],
        "meta": {"total": 4.0, "id": "00000000-0000-0000-0000-000000000001"},
    }
    dumps_strict(result)


def test_primitives_pass_through_unchanged():
    assert serialize_value(None) is None
    assert serialize_value("text") == "text"
    assert serialize_value(7) == 7
    assert serialize_value(True) is True


def test_unknown_object_degrades_to_text():
    class Weird:
        __slots__ = ()

        def __str__(self):
            return "weird"

    assert serialize_value(Weird()) == "weird"


def test_full_deals_row_is_json_serializable():
    """End-to-end shape of the row that triggered the bug."""
    row = {
        "id": 12,
        "title": "Acme renewal",
        "amount": Decimal("24500.00"),
        "currency": "USD",
        "stage": "negotiation",
        "expected_close_date": date(2026, 9, 30),
        "created_at": datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc),
    }
    serialized = {k: serialize_value(v) for k, v in row.items()}
    assert json.loads(dumps_strict(serialized))["amount"] == 24500.0
