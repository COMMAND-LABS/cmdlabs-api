"""The parts every credential endpoint repeated.

Each of the eight endpoints in this package existed as a `(legacy)` / `full`
pair — GET /{id} and GET /{id}/full, POST / and POST /flexible, and so on. The
two halves of each pair differ ONLY in which shape they decrypt into
(`api_key` vs the whole `credential_data` dict), and every one of the eight
restated the same owner lookup, the same 404, the same field-by-field response
construction, and the same ValueError -> 400.

BOTH ROUTES IN EACH PAIR STILL EXIST and still return their own shape. Only
the machinery underneath is shared. Collapsing the pair into a single route
would be an API change; this is not one.

The lookup is by (id, account_id) — credentials are scoped per ACCOUNT rather
than per org, unlike the CRM tables. That is deliberate here: a credential is
a secret belonging to whoever added it, and it reaches other members through
an explicit grant (services/credential_access) rather than by org membership.
Keeping it in one function is what makes that reviewable.
"""
import logging
from contextlib import contextmanager

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from src.db.models import Credential
from src.db.service_name import ServiceName

from .encryption import decrypt_credential_data, get_credential_value
from .models import CredentialDetailResponse, CredentialResponse, FlexibleCredentialDetailResponse

logger = logging.getLogger(__name__)


def owned_credential_or_404(
    db: Session,
    account_id: int,
    *,
    credential_id: int | None = None,
    service_name: ServiceName | None = None,
) -> Credential:
    """The caller's credential, by id or by service type, or 404.

    Exactly one of `credential_id` / `service_name` is given. Both forms filter
    on account_id, which is the ownership check — omitting it would hand any
    caller any credential in the database.
    """
    query = db.query(Credential).filter(Credential.account_id == account_id)
    if credential_id is not None:
        credential = query.filter(Credential.id == credential_id).first()
        missing = "Credential not found"
    else:
        credential = query.filter(Credential.credential_type == service_name).first()
        missing = f"Credential for service '{service_name.value}' not found"

    if not credential:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=missing)
    return credential


@contextmanager
def invalid_data_as_400(db: Session, action: str):
    """Turn a decrypt/encrypt ValueError into a 400 without leaking the reason.

    A ValueError here means the stored ciphertext will not decrypt or the
    supplied data will not serialize — a client-visible problem, but the
    exception text can carry key material, so only the log gets it.
    """
    try:
        yield
    except ValueError as e:
        db.rollback()
        logger.error('[CREDENTIALS] ValueError %s: %s', action, e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail='Invalid credential data.',
        )


def _common(credential: Credential) -> dict:
    """The fields all three response shapes share.

    `created_at`/`updated_at` are isoformat strings rather than datetimes
    because the response models declare them as `str`. Left as-is: changing the
    annotation to datetime would serialize to the same ISO-8601 JSON, but that
    is an API-surface question rather than a cleanup, so it stays explicit here
    in ONE place instead of eight.
    """
    return dict(
        id=credential.id,
        credential_type=credential.credential_type,
        credential_name=credential.credential_name,
        created_at=credential.created_at.isoformat(),
        updated_at=credential.updated_at.isoformat(),
        credential_metadata=credential.credential_metadata,
    )


def metadata_response(credential: Credential) -> CredentialResponse:
    """Credential WITHOUT any secret — what create/update return."""
    return CredentialResponse(auth_type=credential.auth_type, **_common(credential))


def legacy_detail_response(credential: Credential) -> CredentialDetailResponse:
    """The `(legacy)` read shape: the decrypted value flattened to `api_key`.

    `auth_type or "api_key"` mirrors what the legacy endpoints did — rows
    predating the auth_type column read as api_key.
    """
    return CredentialDetailResponse(
        auth_type=credential.auth_type or "api_key",
        api_key=get_credential_value(credential, "api_key"),
        **_common(credential),
    )


def flexible_detail_response(credential: Credential) -> FlexibleCredentialDetailResponse:
    """The `/full` read shape: the whole decrypted dict."""
    return FlexibleCredentialDetailResponse(
        auth_type=credential.auth_type,
        credential_data=decrypt_credential_data(credential.encrypted_data),
        **_common(credential),
    )
