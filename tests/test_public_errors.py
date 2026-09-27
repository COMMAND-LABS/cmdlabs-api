"""The helpers that turn third-party exceptions into client-safe text."""
from sqlalchemy.exc import ArgumentError, OperationalError, ProgrammingError

from src.agent_runtime.tools.exceptions import db_error_for_model
from src.utils.errors import own_message_or_reason, public_reason


class _ClientError(Exception):
    """Shaped like botocore's ClientError: the AWS code lives on .response."""

    def __init__(self, code, message):
        super().__init__(message)
        self.response = {"Error": {"Code": code, "Message": message}}


def test_aws_errors_become_their_code():
    exc = _ClientError("SignatureDoesNotMatch", "... Credential=AKIAEXAMPLEKEY/20260927 ...")
    assert public_reason(exc) == "SignatureDoesNotMatch"


def test_other_errors_become_their_class_name():
    assert public_reason(RuntimeError("password=hunter2")) == "RuntimeError"


def test_our_own_value_errors_keep_their_message():
    assert own_message_or_reason(ValueError("Unsupported LLM provider: foo")) == \
        "Unsupported LLM provider: foo"


def test_value_error_subclasses_do_not():
    class ValidationError(ValueError):
        pass

    assert own_message_or_reason(ValidationError("api_key='sk-secret'")) == "ValidationError"


def test_db_statement_errors_keep_the_drivers_first_line():
    orig = Exception('column "nme" does not exist\nLINE 1: SELECT nme FROM t')
    exc = ProgrammingError("SELECT nme FROM t", {}, orig)
    assert db_error_for_model(exc) == 'ProgrammingError: column "nme" does not exist'


def test_db_connection_errors_do_not_leak_the_connection_string():
    url_error = ArgumentError("Could not parse SQLAlchemy URL from string 'postgresql://u:hunter2@h/db'")
    conn_error = OperationalError("connect", {}, Exception('password authentication failed for user "u"'))
    for exc in (url_error, conn_error):
        text = db_error_for_model(exc)
        assert "hunter2" not in text and '"u"' not in text
        assert text.startswith("Database error (")
