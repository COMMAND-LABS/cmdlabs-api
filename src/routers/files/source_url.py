"""
Source-document signed-URL endpoint (agent-scoped).

A vector-search result points back to the original source file in GCS. When the
agent (and therefore its knowledge base) is SHARED with an account via an access
group, that member must be able to open the source — but they hold no GCS
credential and the file lives in the OWNER's bucket. This endpoint signs the URL
with the owner's key, authorized by the member's access to the agent.

Security model (defense in depth — the signed URL is the only thing the member
ever receives; the credential never leaves the server):
  1. The caller must be able to access the agent (can_access_agent).
  2. The owner is the agent's owner; the agent's vector tools only ever search
     the owner's knowledge bases. The caller must be able to READ the knowledge
     base the file belongs to (owner or read grant) — the same gate the
     vector-search tool is built behind, so anyone shown a chunk can open its
     source, whoever wrote it, and nobody else can.
  3. The requested object path MUST be a source file of a knowledge base
     namespace one of the agent's vector-search tools actually searches
     (vector_stores/{index}/{namespace}/{batch}/{file_id}/{filename}, the only
     layout uploads write). This prevents an authorized member from signing
     arbitrary objects in the owner's bucket.
  4. When the owner's ingestion log recorded a bucket for the path, that bucket
     is used, so source files stay reachable even if the owner later swaps GCS
     credentials. Otherwise (the log is best-effort, and notes approved before
     knowledgeWrite logged have no row) the knowledge base's bound credential
     signs — the same one the upload stored the file with.
"""
import logging

from fastapi import APIRouter, Request, Query, HTTPException, status

from src.deps import jwt_dependency, db_dependency, org_dependency, account_id_from_claims
from src.db.models import Agent, VectorDbIngestionLog
from src.services.agent_access import can_access_agent
from src.services.agent_knowledge_bases import vector_tool_configs
from src.services.vector_store_access import can_read_vector_store
from src.services import account_gcs_service
from src.services.account_gcs_service import AccountGcsCredentialMissing
from src.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

# Segments after vector_stores/{index}/{namespace}/: batch, file id, filename.
_SOURCE_SUFFIX_SEGMENTS = 3


def _agent_kb_scopes(agent: Agent) -> set:
    """(index, namespace) pairs searched by the agent's vector-search tools."""
    return {
        (t["index"], t["namespace"])
        for t in vector_tool_configs(agent.config)
        if t.get("namespace")
    }


def _index_for_path(path: str, scopes: set) -> str | None:
    """The index whose searched namespace holds source file ``path``, or None.

    Matches the exact upload layout, so a namespace that is a prefix of another
    ('a' vs 'a/b') cannot reach the other's files.
    """
    if "\\" in path or any(seg in ("", ".", "..") for seg in path.split("/")):
        return None
    for index, namespace in scopes:
        prefix = f"vector_stores/{index}/{namespace}/"
        if path.startswith(prefix) and len(path[len(prefix):].split("/")) == _SOURCE_SUFFIX_SEGMENTS:
            return index
    return None


@router.get("/source-url")
@limiter.limit("120/minute")
async def get_source_url(
    request: Request,
    agent_id: int = Query(..., description="Agent the source was retrieved through"),
    path: str = Query(..., description="Object path of the source file (bucket-relative)"),
    expires: int = Query(900, ge=60, le=3600, description="URL lifetime in seconds"),
    db: db_dependency = None,
    decoded_jwt: jwt_dependency = None,
    org: org_dependency = None,
):
    """Return a short-lived signed GET URL for an agent's source document."""
    account_id = account_id_from_claims(decoded_jwt)

    if not path or not path.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A path is required")
    path = path.strip()

    # 1. Access check (owner or shared-via-group both pass here).
    if not can_access_agent(db, account_id, agent_id, org_id=org.org_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found")

    # 2 + 3. The path must be a source file of a namespace the agent searches.
    owner_account_id = agent.account_id
    index_name = _index_for_path(path, _agent_kb_scopes(agent))
    if not index_name or not can_read_vector_store(
        db, account_id, index_name, owner_account_id, org_id=org.org_id
    ):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source document not found")

    # 4. Prefer the bucket recorded at ingest.
    row = (
        db.query(VectorDbIngestionLog)
        .filter(
            VectorDbIngestionLog.account_id == owner_account_id,
            VectorDbIngestionLog.gcs_file_path == path,
            VectorDbIngestionLog.index_name == index_name,
            VectorDbIngestionLog.gcs_bucket.isnot(None),
        )
        .order_by(VectorDbIngestionLog.created_at.desc())
        .first()
    )

    try:
        if row:
            url = account_gcs_service.generate_signed_url_for(
                db,
                owner_account_id,
                gcs_bucket=row.gcs_bucket,
                gcs_file_path=path,
                expiration_seconds=expires,
            )
        else:
            url = account_gcs_service.generate_signed_url_for_index(
                db,
                owner_account_id,
                index_name,
                gcs_file_path=path,
                expiration_seconds=expires,
            )
    except AccountGcsCredentialMissing as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    return {"url": url, "expires_in": expires}
