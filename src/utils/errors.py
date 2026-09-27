"""
Centralised database / request error handling.

DO NOT wrap a whole endpoint in `try/except Exception: raise
handle_db_error(...)`. That tail used to sit at the bottom of ~150 endpoints
and it is redundant now: main.py registers app-level handlers producing the
same responses, get_db rolls back, and a router that simply lets the exception
go gets the identical status code with none of the indentation.

What is left for this function is the narrow case: a block that must catch
something SPECIFIC (a ValueError to turn into a 400, a Stripe failure) and
wants the same safe mapping for whatever else shows up alongside it.

    try:
        plan = parse_plan(body.plan)          # raises ValueError
    except ValueError:
        raise HTTPException(status_code=400, detail="Unknown plan")
    except StripeError as e:
        raise handle_db_error(e, "[CHECKOUT]")
"""
import logging
from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

logger = logging.getLogger(__name__)


def handle_db_error(e: Exception, log_prefix: str) -> HTTPException:
    """
    Map a caught exception to a safe HTTPException.

    - Logs the full error server-side.
    - Never leaks internal error strings (SQL, stack traces, etc.) to the client.
    - Returns meaningful HTTP status codes for known constraint violations.
    """
    logger.error("%s %s: %s", log_prefix, type(e).__name__, e)

    if isinstance(e, IntegrityError):
        orig = getattr(e, "orig", None)
        msg = str(orig).lower() if orig else str(e).lower()

        if "unique" in msg or "duplicate" in msg:
            return HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A record with that value already exists.",
            )
        if "foreign key" in msg or "violates foreign" in msg:
            return HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The request references a resource that does not exist.",
            )
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The request conflicts with existing data.",
        )

    if isinstance(e, SQLAlchemyError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="A database error occurred. Please try again.",
        )

    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="An unexpected error occurred. Please try again.",
    )


def public_reason(exc: BaseException) -> str:
    """A client-safe description of a third-party failure.

    The raw text of an SDK / driver / crypto exception can carry secrets —
    AWS signature errors echo the access key id, SQLAlchemy URL errors echo the
    connection string, LLM SDKs echo (partly masked) API keys. So text from
    those never reaches a response, an SSE frame, a tool result the model sees,
    or a row the API later returns; log the exception and send this instead.

    boto3/botocore ClientError -> its AWS error code (e.g. "MessageRejected",
    "Throttling"), which is useful and never secret. Anything else -> the
    exception class name.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code:
            return str(code)
    return type(exc).__name__


def own_message_or_reason(exc: BaseException) -> str:
    """The message of a ValueError this codebase raised, else public_reason.

    Our validation code raises PLAIN ValueErrors with messages written for the
    user ("Unsupported LLM provider", "vectorSearch requires an index"), so
    those are shown as-is. Library code raises ValueError SUBCLASSES — notably
    pydantic's ValidationError, whose text echoes its inputs (an API key, a
    connection string) — and those are reduced to a safe reason.
    """
    if type(exc) is ValueError:
        return str(exc)
    return public_reason(exc)
