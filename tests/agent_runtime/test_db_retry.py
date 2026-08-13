"""Tests for the transient DB error retry helper."""

from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from src.db.retry import db_retry_once, is_transient_db_error


def test_succeeds_without_retry():
    db = MagicMock()
    result = db_retry_once(db, "test", lambda: 42)
    assert result == 42
    db.rollback.assert_not_called()


def test_retries_on_ssl_error():
    db = MagicMock()
    call_count = 0

    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise OperationalError(
                "select",
                {},
                Exception("SSL connection has been closed unexpectedly"),
            )
        return "recovered"

    result = db_retry_once(db, "test", flaky)
    assert result == "recovered"
    assert call_count == 2
    db.close.assert_called_once()


def test_raises_non_transient_errors():
    db = MagicMock()

    def failing():
        raise OperationalError("select", {}, Exception("relation does not exist"))

    with pytest.raises(OperationalError):
        db_retry_once(db, "test", failing)


def test_is_transient_detects_known_patterns():
    assert is_transient_db_error(Exception("SSL connection has been closed unexpectedly"))
    assert is_transient_db_error(Exception("server closed the connection unexpectedly"))
    assert is_transient_db_error(Exception("connection reset by peer"))
    assert is_transient_db_error(Exception("could not receive data from server"))


def test_is_transient_rejects_other_errors():
    assert not is_transient_db_error(Exception("relation does not exist"))
    assert not is_transient_db_error(Exception("syntax error"))
