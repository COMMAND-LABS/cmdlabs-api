"""Shared exceptions for the tools package."""

from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError

from src.utils.errors import public_reason


class CredentialError(Exception):
    """Raised when a credential required by a tool is missing, invalid, or inaccessible."""


def db_error_for_model(exc: Exception) -> str:
    """What a database tool tells the model when a statement fails.

    Statement errors (unknown column, bad type, constraint violation) keep the
    driver's first line: the model needs it to correct its query, and it
    describes the query rather than the connection. Everything else —
    connection and authentication failures, whose text names hosts and users,
    and URL errors, which echo the whole connection string — is reduced to a
    safe reason. The full exception is for the log only.
    """
    if isinstance(exc, (ProgrammingError, DataError, IntegrityError)):
        lines = str(getattr(exc, "orig", None) or exc).strip().splitlines()
        if lines:
            return f"{type(exc).__name__}: {lines[0]}"
    return f"Database error ({public_reason(exc)})"
