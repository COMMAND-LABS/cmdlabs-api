"""Dataset references for the runner-backed tools.

A `datasetRef` in the agent config names a CSV in the agent OWNER's bucket
(the policy every read tool follows: shared members use the owner's data
through the owner's credential). The bytes are fetched at CALL time over a
short-lived session — tools run after the request session is closed — and
handed to the runner in the request body.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from src.db.service_name import ServiceName
from src.services import account_gcs_service
from src.services.credential_access import resolve_default_credential

from .exceptions import CredentialError


@dataclass(frozen=True)
class DatasetRef:
    gcs_path: str
    filename: str


def parse_dataset_ref(raw: dict[str, Any]) -> DatasetRef:
    gcs_path = (raw or {}).get("gcsPath", "").strip()
    if not gcs_path:
        raise ValueError("dataset.gcsPath is required")
    filename = (raw.get("filename") or posixpath.basename(gcs_path)).strip()
    if not filename or filename in (".", "..") or "/" in filename:
        raise ValueError(f"dataset filename {filename!r} is not a plain file name")
    return DatasetRef(gcs_path=gcs_path, filename=filename)


def require_gcs_credential(db: Session, owner_account_id: int) -> None:
    """Fail at BUILD time, not on the first call, when the owner has no GCS
    credential — the same moment the email tools verify theirs."""
    if not resolve_default_credential(db, owner_account_id, ServiceName.GOOGLE_CLOUD_STORAGE):
        raise CredentialError(
            "The agent owner has no Google Cloud Storage credential configured; "
            "dataset-backed tools need one to read the dataset."
        )


async def fetch_dataset(
    session_factory: Callable[[], Session],
    owner_account_id: int,
    ref: DatasetRef,
) -> bytes:
    """Download the dataset bytes on a worker thread over a short-lived session."""
    def _download() -> bytes:
        db = session_factory()
        try:
            return account_gcs_service.download_bytes(db, owner_account_id, gcs_file_path=ref.gcs_path)
        finally:
            db.close()

    return await asyncio.to_thread(_download)
