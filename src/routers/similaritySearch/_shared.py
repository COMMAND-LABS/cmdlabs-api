"""
Tenancy for the similarity-search endpoints.

Everything here lives in ONE shared Pinecone index, and a namespace in it is
shared by every account: `prompts` holds every account's prompts, `crm` every
account's contact history. A namespace is therefore never an access boundary —
each vector's owner metadata is. So:

  - only the namespaces this module serves may be named at all (`crm` and any
    other namespace are refused), and
  - every read and delete is narrowed to vectors the CALLER owns, using the
    owner field each ingest path writes: prompts carry `account_id` (int,
    routers/prompts/_shared.py); similarity-search CSV rows carry `user_id`
    (the caller's id as a string, set by the Q&A ingest function from the
    upload message).
"""

SIMILARITY_SEARCH_NAMESPACE = "similarity_search"


def _prompt_owner(account_id: int) -> dict:
    return {"account_id": {"$eq": account_id}}


def _upload_owner(account_id: int) -> dict:
    return {"user_id": {"$eq": str(account_id)}}


# namespace -> Pinecone metadata filter selecting the caller's own vectors.
_OWNER_FILTERS = {
    "prompts": _prompt_owner,
    SIMILARITY_SEARCH_NAMESPACE: _upload_owner,
}

SEARCHABLE_NAMESPACES = tuple(_OWNER_FILTERS)


def owner_filter(namespace: str, account_id: int) -> dict | None:
    """The metadata filter restricting `namespace` to the caller's vectors, or
    None when the namespace may not be searched through this API."""
    make = _OWNER_FILTERS.get(namespace)
    return make(account_id) if make else None


def is_upload_owned_by(meta: dict | None, account_id: int) -> bool:
    """True when a similarity_search vector was uploaded by `account_id`."""
    return bool(meta) and str(meta.get("user_id")) == str(account_id)
