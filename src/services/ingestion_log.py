"""
Best-effort VectorDbIngestionLog writes.

The upload, delete and knowledgeWrite paths all record their vector DB
operation in the ingestion log, and all of them treat that write as
best-effort: the operation itself has already happened, so a failure to log it
must never fail the request. This is the one place that policy lives.
"""
import logging
from typing import Optional

from sqlalchemy.orm import Session

from src.db.models import VectorDbIngestionLog

logger = logging.getLogger(__name__)


def record_ingestion_log(
    db: Session,
    *,
    log_prefix: str,
    refresh: bool = False,
    **fields,
) -> Optional[str]:
    """Insert and commit one VectorDbIngestionLog row built from ``fields``.

    Returns the new row's id as a string when ``refresh`` is True (the row is
    refreshed after commit so the id can be read), otherwise None. On any
    failure the session is rolled back, a warning is logged under
    ``log_prefix``, and None is returned — the exception is never raised.
    """
    try:
        ingestion_log = VectorDbIngestionLog(**fields)
        db.add(ingestion_log)
        db.commit()
        if not refresh:
            return None
        db.refresh(ingestion_log)
        return str(ingestion_log.id)
    except Exception as e:
        logger.warning("%s Failed to create ingestion log entry: %s: %s", log_prefix, type(e).__name__, e)
        db.rollback()
        return None
