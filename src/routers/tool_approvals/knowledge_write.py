"""Execute an approved knowledgeWrite: store the note and queue its ingestion.

The note becomes a small markdown file with YAML front matter in the KB's
bucket (the txt-ingest function parses front matter into `file_*` metadata,
so the topic is searchable) and a message on txt-ingest-topic. Ingestion is
asynchronous; "approved" here means "stored and queued", not "searchable".

Like the CSV/TXT upload routes, each approved note gets a PENDING
VectorDbIngestionLog row under the KB owner, carrying the GCS pointer. That row
is what surfaces the note on the knowledge base detail page and what the
source-URL endpoints check before signing a link to the original file.

Authorization is re-checked at approval time. The tool checked it when the
agent turn was built, but a grant can be revoked between the two.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from src.db.models import PendingToolApproval, VectorDbIngestionLog
from src.services import account_gcs_service
from src.services.vector_store_access import authorize_vector_store
from src.services.vector_stores_upload_service import TXT_INGEST_TOPIC, VectorStoresUploadService

logger = logging.getLogger(__name__)

_TOPIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def render_note(*, topic: str, text: str, author_email: str, approved_at: datetime) -> str:
    """Markdown with front matter. Values are quoted so an email or a colon in
    them cannot break the YAML the ingest function parses."""
    return (
        "---\n"
        f"topic: \"{topic}\"\n"
        f"author: \"{author_email}\"\n"
        f"approved_at: \"{approved_at.isoformat()}\"\n"
        "source: \"agent knowledge_write\"\n"
        "---\n\n"
        f"{text.strip()}\n"
    )


async def execute_knowledge_write(
    db: Session,
    approval: PendingToolApproval,
    *,
    account_id: int,
    user_email: str,
    jwt: str | None,
    text_override: str | None = None,
) -> str:
    """Store + queue the note. Marks the approval approved on success and
    returns the user-facing message. Raises HTTPException on failure and
    leaves the approval pending so it can be retried or rejected."""
    payload = approval.payload or {}
    index_name = payload.get("index")
    namespace = payload.get("namespace")
    topic = payload.get("topic", "")
    owner_account_id = payload.get("owner_account_id")
    text = (text_override or "").strip() or (payload.get("text") or "").strip()

    if not all([index_name, namespace, owner_account_id]) or not _TOPIC.match(topic):
        raise HTTPException(status_code=422, detail="Approval payload is incomplete.")
    if not text:
        raise HTTPException(status_code=422, detail="The note is empty.")

    authorize_vector_store(db, account_id, index_name, owner_account_id, require_write=True)

    now = datetime.now(timezone.utc)
    content = render_note(topic=topic, text=text, author_email=user_email, approved_at=now)
    filename = f"{topic}-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.md"
    comment = f"knowledge_write by {user_email} (approval {approval.id})"
    # Chosen here, not by the upload service, so the log row and the Pub/Sub
    # message carry the same batch_number.
    batch_number = str(uuid.uuid4())

    try:
        result = await VectorStoresUploadService().upload_bytes_and_publish(
            file_bytes=content.encode("utf-8"),
            filename=filename,
            content_type="text/markdown",
            user_id=str(account_id),
            user_email=user_email,
            index_name=index_name,
            namespace=namespace,
            jwt=jwt,
            db=db,
            account_id=owner_account_id,
            topic_name=TXT_INGEST_TOPIC,
            batch_number=batch_number,
            comment=comment,
        )
    except account_gcs_service.AccountGcsCredentialMissing as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not result.get("success"):
        logger.error("knowledgeWrite approval %s: upload failed: %s", approval.id, result)
        raise HTTPException(status_code=500, detail="Failed to store the note. Please try again.")

    approval.status = "approved"
    db.commit()
    logger.info("knowledgeWrite approval %s stored as %s", approval.id, result.get("gcs_file_path"))

    # Logged after the approval commits: the note is already stored and queued,
    # so a logging failure must not leave the approval pending (a retry would
    # upload a duplicate). Same best-effort policy as the upload routes.
    try:
        db.add(VectorDbIngestionLog(
            account_id=owner_account_id,
            provider="pinecone",
            index_name=index_name,
            namespace=namespace,
            filenames=[filename],
            comment=comment,
            gcs_bucket=result.get("gcs_bucket"),
            gcs_file_path=result.get("gcs_file_path"),
            operation_type="INGEST",
            status="PENDING",
            vectors_added=0,
            vectors_deleted=0,
            vectors_failed=0,
            batch_number=batch_number,
        ))
        db.commit()
    except Exception as e:
        logger.warning("knowledgeWrite approval %s: failed to create ingestion log entry: %s: %s",
                       approval.id, type(e).__name__, e)
        db.rollback()

    return f"Note saved under '{topic}' and queued for ingestion into {index_name}/{namespace}."
