"""What the transactional senders leave behind in the production logs.

These assert on the *log line*, not on SES, because the log line is the product:
when a sign-in code does not arrive, this text is the only evidence of which of
the three cases it was — never ran, ran and was accepted, ran and was rejected.
"""
import logging

import pytest
from botocore.exceptions import ClientError, NoCredentialsError

from src.routers.auth.background_tasks import send_login_code_email_ses as sender
from src.services import ses_logging

SES_OK = {
    "MessageId": "010f-msg-id",
    "ResponseMetadata": {"RequestId": "req-ok", "HTTPStatusCode": 200},
}


class _FakeSes:
    """Stands in for the boto3 SES client: returns a response or raises one."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    def send_email(self, **kwargs):
        if self._error is not None:
            raise self._error
        return self._response


@pytest.fixture
def ses(monkeypatch):
    """Swap the sender's boto3 for a double, the same seam test_lead_magnets uses."""
    def _install(response=None, error=None):
        client = _FakeSes(response, error)
        monkeypatch.setattr(sender.boto3, "client", lambda *a, **k: client)
    return _install


def _lines(caplog):
    return [r.message for r in caplog.records if r.message.startswith("ses_send")]


# ── the three cases the logs have to tell apart ───────────────────────────────

def test_successful_send_logs_attempt_then_accepted_with_message_id(ses, caplog):
    ses(response=SES_OK)
    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "12345678")

    attempt, accepted = _lines(caplog)
    assert "status=attempt" in attempt
    assert "kind=login_code" in attempt
    assert "to=user@example.com" in attempt
    # The MessageId is what joins this log to SES's own delivery/bounce records.
    assert "status=accepted" in accepted
    assert "message_id=010f-msg-id" in accepted
    assert "request_id=req-ok" in accepted


def test_ses_rejection_logs_the_error_code_and_message(ses, caplog):
    ses(error=ClientError(
        {"Error": {"Code": "MessageRejected",
                   "Message": "Email address is not verified."},
         "ResponseMetadata": {"RequestId": "req-bad", "HTTPStatusCode": 400}},
        "SendEmail",
    ))
    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "12345678")

    failed = _lines(caplog)[-1]
    # The code names the fix; without it the log says only "it threw".
    assert "status=failed" in failed
    assert "error_code=MessageRejected" in failed
    assert "http_status=400" in failed
    assert "request_id=req-bad" in failed
    assert "Email address is not verified." in failed


def test_missing_credentials_are_reported_by_variable_name(ses, caplog, monkeypatch):
    monkeypatch.delenv("AWS_SECRET_KEY", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    ses(error=NoCredentialsError())

    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "12345678")

    failed = _lines(caplog)[-1]
    assert "error_type=NoCredentialsError" in failed
    # An unset variable is the usual cause and the traceback never names it.
    assert "env_secret=MISSING" in failed
    assert "env_region=set" in failed


# ── invariants that keep the logs safe and the caller unbroken ────────────────

def test_a_failed_send_never_raises_at_the_caller(ses):
    """Senders run as background tasks after the response is already committed."""
    ses(error=RuntimeError("ses down"))
    sender.send_login_code_email_ses("user@example.com", "12345678")  # must not raise


def test_secret_values_are_never_logged(ses, caplog, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_KEY", "super-secret-value")
    ses(error=NoCredentialsError())
    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "12345678")

    assert "super-secret-value" not in "\n".join(_lines(caplog))


def test_the_sign_in_code_is_never_logged(ses, caplog):
    """The log is a delivery record, not a copy of the credential it carried."""
    ses(response=SES_OK)
    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "87654321")

    assert "87654321" not in "\n".join(_lines(caplog))


def test_a_send_email_response_without_metadata_still_logs_accepted(ses, caplog):
    """Older stubs and non-dict returns must not turn a good send into a crash."""
    ses(response=None)
    with caplog.at_level(logging.INFO):
        sender.send_login_code_email_ses("user@example.com", "12345678")

    assert "status=accepted" in _lines(caplog)[-1]


def test_newlines_in_an_ses_message_cannot_break_the_line_format(caplog):
    log = logging.getLogger("test.ses")
    with caplog.at_level(logging.INFO):
        ses_logging.log_failed(
            log, kind="login_code", to_email="user@example.com",
            source="noreply@cmdlabs.io", exc=RuntimeError("line one\nline two"),
        )

    failed = _lines(caplog)[-1]
    assert "\n" not in failed
    assert 'detail="line one line two"' in failed
