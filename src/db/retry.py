"""Transient DB error retry helper.

Used by the agent routers to handle SSL/connection resets that occur
intermittently with Supabase poolers and Cloud Run cold starts.
"""

import contextlib
import logging
import time

from sqlalchemy.exc import OperationalError

logger = logging.getLogger(__name__)

_TRANSIENT_PATTERNS = (
    "ssl connection has been closed unexpectedly",
    "server closed the connection unexpectedly",
    "connection reset by peer",
    "could not receive data from server",
)


def is_transient_db_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(p in text for p in _TRANSIENT_PATTERNS)


def db_retry_once(db, label: str, fn):
    """Execute *fn*; on a transient SSL/connection error, rollback, close, and retry once."""
    try:
        return fn()
    except OperationalError as exc:
        if not is_transient_db_error(exc):
            raise
        logger.warning(f"[DB RETRY] Transient error during {label}; retrying once...")
        with contextlib.suppress(Exception):
            db.rollback()
        db.close()
        time.sleep(0.5)
        return fn()
