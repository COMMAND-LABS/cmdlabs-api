"""Structured logging for the transactional SES senders.

WHY THIS EXISTS
---------------
The five transactional senders (login code, password reset link, password-reset
confirmation, org invitation, lead magnet) each wrapped their SES call in a bare
``except Exception: logger.exception(...)``. That told us *something* broke but
not what, and — worse — said nothing at all on the happy path. A silent log meant
one of three very different things, with no way to tell them apart in prod:

  1. the sender never ran (background task dropped, caller never reached it),
  2. the sender ran and SES accepted the message (so the problem is downstream:
     bounce, complaint, suppression list, spam folder),
  3. the sender ran and SES rejected it (unverified identity, sandbox, bad creds).

So every send now emits a line on entry and a line on the way out. Three
statuses, one grep-able prefix:

    ses_send status=attempt   kind=... to=... source=...
    ses_send status=accepted  kind=... to=... message_id=... request_id=...
    ses_send status=failed    kind=... to=... error_code=... detail="..."

``accepted`` is deliberately not called "sent": SES returning a MessageId means
it took custody of the message, nothing more. Delivery is a separate question
answered by the configuration set's SNS notifications (see READMEs/ses/
setting_up_a_config_set.md), and the MessageId logged here is the key that joins
the two.

WHY A LOGGING HELPER AND NOT A SEND HELPER
------------------------------------------
Each sender keeps building its own ``boto3.client`` and calling ``send_email``
itself. Tests monkeypatch ``<sender_module>.boto3`` to intercept the call
(tests/test_lead_magnets.py), and that seam is worth more than the handful of
duplicated client lines it costs. Only the part that actually benefits from
being written once — pulling the useful fields out of a botocore error — lives
here.

The caller passes its own ``logger`` so the record still carries the sending
module's name, not this one's.
"""
import logging
import os
from typing import Any, Mapping, Optional

_PREFIX = "ses_send"

# Read at call time, never logged by value — only whether each is present. A
# secret that shows up in Cloud Logging is a secret that has to be rotated.
# Mapped to short log keys so the failure line stays one flat level of
# key=value — `env_secret=MISSING` is greppable in a way that a nested
# `credentials=AWS_SECRET_KEY=MISSING` is not.
_CREDENTIAL_ENV_VARS = {
    "env_region": "AWS_REGION",
    "env_key_id": "AWS_ACCESS_KEY_ID",
    "env_secret": "AWS_SECRET_KEY",
}


def _fmt(value: Any) -> str:
    """Render one value for a ``key=value`` log line, quoting only when needed."""
    text = "-" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if not text:
        return '""'
    return f'"{text}"' if (" " in text or '"' in text) else text


def _line(status: str, kind: str, to_email: str, **fields: Any) -> str:
    parts = [
        _PREFIX,
        f"status={status}",
        f"kind={kind}",
        f"to={_fmt(to_email)}",
    ]
    parts += [f"{k}={_fmt(v)}" for k, v in fields.items() if v is not None]
    return " ".join(parts)


def credential_state() -> dict:
    """``set``/``MISSING`` per AWS env var — never the values themselves.

    Logged on failure because the most common cause of a dead transactional
    email in a fresh environment is an unset variable, and the resulting
    ``NoCredentialsError`` traceback does not name which one. Note the
    non-standard ``AWS_SECRET_KEY`` (not ``AWS_SECRET_ACCESS_KEY``) — that
    spelling is what every sender reads, so that is what is checked. An unset
    variable makes boto3 fall back to the default credential chain, which on
    Cloud Run finds nothing and fails at call time rather than at startup.
    """
    return {
        key: ("set" if os.getenv(env_var) else "MISSING")
        for key, env_var in _CREDENTIAL_ENV_VARS.items()
    }


def _unwrap_botocore_error(exc: BaseException) -> dict:
    """Pull the fields worth having out of a botocore ``ClientError``.

    Anything else (``NoCredentialsError``, ``EndpointConnectionError``, a plain
    ``RuntimeError`` from a test double) has no ``response``, so it falls
    through to just its type and ``str()`` — which is still the answer for the
    credential and networking cases.
    """
    out: dict = {"error_type": type(exc).__name__, "detail": str(exc) or None}

    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error") or {}
        if isinstance(error, Mapping):
            # e.g. MessageRejected, MailFromDomainNotVerified,
            # AccountSendingPausedException — the code names the fix.
            out["error_code"] = error.get("Code")
            if error.get("Message"):
                out["detail"] = error.get("Message")
        metadata = response.get("ResponseMetadata") or {}
        if isinstance(metadata, Mapping):
            out["http_status"] = metadata.get("HTTPStatusCode")
            out["request_id"] = metadata.get("RequestId")
    return out


def log_attempt(log: logging.Logger, *, kind: str, to_email: str, source: str) -> None:
    """Emitted before the SES call, so a send that never returns is still visible."""
    log.info(_line("attempt", kind, to_email, source=source,
                   region=os.getenv("AWS_REGION")))


def log_accepted(
    log: logging.Logger,
    *,
    kind: str,
    to_email: str,
    source: str,
    response: Optional[Mapping[str, Any]] = None,
) -> None:
    """Emitted when SES takes custody. ``message_id`` is the correlation key."""
    message_id = None
    request_id = None
    if isinstance(response, Mapping):
        message_id = response.get("MessageId")
        metadata = response.get("ResponseMetadata") or {}
        if isinstance(metadata, Mapping):
            request_id = metadata.get("RequestId")
    log.info(_line("accepted", kind, to_email, source=source,
                   message_id=message_id, request_id=request_id))


def log_failed(
    log: logging.Logger,
    *,
    kind: str,
    to_email: str,
    source: str,
    exc: BaseException,
) -> None:
    """Emitted when the SES hand-off raised. Keeps the traceback *and* the fields.

    ``exc_info`` is passed explicitly rather than using ``logger.exception`` so
    this works the same whether or not it is called from an ``except`` block.
    """
    fields = _unwrap_botocore_error(exc)
    log.error(
        _line("failed", kind, to_email, source=source,
              **credential_state(), **fields),
        exc_info=exc,
    )
