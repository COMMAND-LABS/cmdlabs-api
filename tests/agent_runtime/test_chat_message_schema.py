"""chat_message.v2 must accept every message the runtime actually stores, and
reject malformed ones.

Tool calls are built with the REAL formatter (helpers/tool_calls.format_tool_call)
from realistic tool outputs, so a formatter/schema drift shows up here rather
than as an "invalid message" log line in production. The schema selects the
tool-call layout by toolType: a typed call must match its own layout (no
catch-all rescue), and every type without a dedicated layout — including the
ones _format_typed passes through — uses the generic one.
"""

import logging
from unittest.mock import MagicMock, patch

import pytest
from jsonschema import ValidationError

from src.agent_runtime.helpers import message_history
from src.agent_runtime.helpers.message_history import store_ai_message, store_user_message
from src.agent_runtime.helpers.tool_calls import format_tool_call
from src.schemas import validate_against_schema


def _valid(message: dict) -> None:
    validate_against_schema(message, "chat_message", 2)


def _ai(*tool_calls: dict, **extra) -> dict:
    return {"role": "ai", "content": "Here you go.", "toolCalls": list(tool_calls), **extra}


# ── Realistic tool outputs (the shapes the tools in agent_runtime/tools return) ──

TEXT_METADATA = {  # txt-ingest text_processor, incl. fields beyond the documented shape
    "filename": "sops.md", "chunkId": 1, "content": "Step 1...", "chunkSizeTokens": 120,
    "uploadTimestamp": "1706234567890", "chunkNumber": 1.0, "totalChunks": 4.0,
    "user_id": "7", "user_email": "a@b.co", "upload_timestamp": "1706234567890",
    "created_at": "1/2/2026", "last_edited_at": "1/2/2026", "storage_provider": "gcs",
    "storage_bucket": "b", "storage_path": "p/sops.md", "file_video_title": "Intro",
}
QA_METADATA = {
    "row_number": 3, "q": "Price?", "a": "$29", "content": "Q: Price?\nA: $29",
    "filename": "faq.csv", "user_id": "7", "user_email": "a@b.co",
    "upload_timestamp": "1706234567890",
}
LEGACY_METADATA = {"filename": "old.md", "content": "...", "gcs_bucket": "b", "gcs_file_path": "p"}

SEARCH_OUTPUT = {
    "results": [
        {"metadata": TEXT_METADATA, "score": 0.83, "id": "a" * 64},
        {"metadata": QA_METADATA, "score": 0.71, "id": "b" * 64},
        {"metadata": LEGACY_METADATA, "score": 1.7, "id": "c" * 64},  # dotproduct index
    ],
    "namespace": "sops",
    "index": "kb",
}
RERANKED_OUTPUT = {
    "results": [{"metadata": QA_METADATA, "score": 0.97, "id": "b" * 64, "similarity_score": 0.71}],
    "namespace": "sops", "index": "kb", "reranking_applied": True,
    "initial_results": 20, "final_results": 1,
}


def _all_emitted_tool_calls() -> list[dict]:
    calls = [
        format_tool_call("search_sops", {"query": "refunds", "top_k": 5},
                         str(SEARCH_OUTPUT), tool_type="vectorSearch"),
        format_tool_call("search_sops", {"query": "nothing"},
                         {"results": [], "message": "No relevant documents found"},
                         tool_type="vectorSearch"),
        format_tool_call("search_rerank", {"query": "price", "top_k": 20, "top_n": 5},
                         str(RERANKED_OUTPUT), tool_type="vectorSearchWithReranking"),
        format_tool_call("query_orders", {"filters": {"status": "open"}, "limit": 10, "offset": 0},
                         {"results": [{"data": {"id": 1, "total": 9.5}}], "table": "orders", "count": 1},
                         tool_type="dbTableRead"),
        # Model omitted every optional arg; and a failed query.
        format_tool_call("query_orders", {}, {"error": "relation does not exist"},
                         tool_type="dbTableRead"),
        format_tool_call("create_lead", {"name": "Ann"},
                         {"success": True, "table": "leads", "inserted": {"id": 4, "name": "Ann"},
                          "message": "Successfully inserted record into leads"},
                         tool_type="dbTableWrite"),
        format_tool_call("create_lead", {"name": "Ann"}, {"error": "Missing required columns: ['email']"},
                         tool_type="dbTableWrite"),
        format_tool_call("send_email", {"to_email": "x@y.co", "subject": "Hi", "body": "Hello"},
                         '{"success": false, "error": "Failed to queue email for approval."}',
                         tool_type="sendTxtEmailWithSes"),
        format_tool_call("send_html", {"to_email": "x@y.co", "subject": "Hi", "template_id": 3,
                                       "variables": {"name": "X"}},
                         {"success": True, "message_id": "0100-abc"},
                         tool_type="sendHtmlEmailWithSes"),
        format_tool_call("send_html", {"to_email": "x@y.co", "subject": "Hi", "html_body": "<p>x</p>"},
                         {"success": False, "error": "SES rejected"}, tool_type="sendHtmlEmailWithSes"),
        # Gmail variants share the plain-email formatter, keeping their own type.
        format_tool_call("send_txt_email_with_google_oauth",
                         {"to_email": "x@y.co", "subject": "Hi", "body": "Hello"},
                         '{"success": false, "error": "Failed to queue email for approval."}',
                         tool_type="sendTxtEmailWithGoogleOAuth"),
        format_tool_call("load_skill", {"skill_name": "triage"}, "# Triage\n...", tool_type="loadSkill"),
        format_tool_call("save_skill", {"name": "triage", "description": "d", "content": "# T"},
                         {"saved": True, "action": "created", "skillId": 12, "name": "triage",
                          "visibility": "private"},
                         tool_type="saveSkill"),
        format_tool_call("forecast_duty_spend", {"dataset": "duties.csv", "horizon": 6},
                         {"forecast": [{"period": "2026-10", "value": 10.5}], "model": "ets"},
                         tool_type="timeSeriesForecast"),
        format_tool_call("run_python", {"code": "print(1)"}, {"stdout": "1\n", "exit_code": 0},
                         tool_type="codeExecution"),
        format_tool_call("save_note", {"text": "x"}, {"error": "Knowledge base not writable."},
                         tool_type="knowledgeWrite"),
        format_tool_call("contact", {}, {"id": 1, "name": "Ann Lee", "email": None, "phone": None},
                         tool_type="contactRead"),
        format_tool_call("contact_events", {"limit": 5}, {"events": []}, tool_type="contactEventsRead"),
        format_tool_call("log_event", {"event_type": "call", "title": "Intro"},
                         {"success": True, "event": {"id": 9, "title": "Intro"}},
                         tool_type="contactEventWrite"),
        format_tool_call("think", {"thought": "hmm"},
                         "Thought recorded. Continue reasoning, or give your final answer.",
                         tool_type="think"),
        format_tool_call("some_untagged_tool", {"a": 1}, 42),  # untagged -> "custom"
    ]
    assert all(c is not None for c in calls)
    return calls


@pytest.mark.parametrize("call", _all_emitted_tool_calls(),
                         ids=lambda c: f"{c['toolType']}:{c['toolName']}")
def test_every_emitted_tool_call_validates(call):
    _valid(_ai(call))


def test_the_expected_layouts_are_emitted():
    types = {c["toolType"] for c in _all_emitted_tool_calls()}
    assert {"vectorSearch", "vectorSearchWithReranking", "dbTableRead", "dbTableWrite",
            "sendTxtEmailWithSes", "sendHtmlEmailWithSes", "custom"} <= types


def test_full_multi_step_ai_message_validates():
    calls = _all_emitted_tool_calls()
    _valid(_ai(*calls, agentName="Support",
               blocks=[{"kind": "text", "content": "Looking."}, {"kind": "tool", "index": 0},
                       {"kind": "text", "content": "Found it."}]))


def test_plain_messages_validate():
    _valid({"role": "ai", "content": "hi"})
    _valid({"role": "human", "content": "hi"})


def test_human_message_with_attachments_validates():
    _valid({"role": "human", "content": "see file", "attachments": [{
        "type": "pdf", "filename": "doc.pdf", "gcs_bucket": "acct-7",
        "gcs_file_path": "chat/1/doc.pdf", "content_type": "application/pdf"}]})
    _valid({"role": "human", "content": "legacy", "attachments": [{"type": "pdf", "filename": "doc.pdf"}]})


# ── Malformed messages must still fail ───────────────────────────────────────

def _fails(message: dict, *needles: str) -> None:
    with pytest.raises(ValidationError) as exc:
        _valid(message)
    for needle in needles:
        assert needle in str(exc.value)


def test_typed_call_missing_required_output_fails():
    # Used to pass as customToolCall via the catch-all.
    _fails(_ai({"toolType": "vectorSearch", "toolName": "s", "input": {"query": "q"},
                "output": {"results": []}}), "namespace")


def test_typed_call_with_wrong_field_type_fails():
    _fails(_ai({"toolType": "dbTableRead", "toolName": "q", "input": {},
                "output": {"results": [], "table": "t", "count": "one"}}), "count")


def test_email_call_without_success_fails():
    _fails(_ai({"toolType": "sendTxtEmailWithSes", "toolName": "e", "input": {}, "output": {}}),
           "success")


def test_tool_call_missing_tool_type_fails():
    _fails(_ai({"toolName": "x", "input": {}, "output": {}}), "toolType")


def test_unknown_top_level_key_fails():
    _fails({"role": "human", "content": "x", "bogus": 1}, "bogus")


def test_attachment_without_type_fails():
    _fails({"role": "human", "content": "x", "attachments": [{"filename": "a.pdf"}]}, "type")


def test_bad_role_fails():
    _fails({"role": "system", "content": "x"}, "system")


# ── message_history validates with a schema that exists ──────────────────────

@pytest.mark.parametrize("kwargs", [
    {},
    {"pdf_filename": "doc.pdf"},
    {"attachment_ref": {"type": "image", "filename": "a.png", "gcs_bucket": "b",
                        "gcs_file_path": "p/a.png", "content_type": None}},
])
def test_store_user_message_logs_no_validation_error(kwargs):
    with patch.object(message_history, "logger") as log:
        result = store_user_message(MagicMock(), session_id=1, prompt="hello", **kwargs)
    assert result is not None
    log.error.assert_not_called()


def test_store_ai_message_logs_no_validation_error():
    with patch.object(message_history, "logger") as log:
        store_ai_message(MagicMock(), session_id=1, content="done",
                         tool_calls=_all_emitted_tool_calls(), agent_name="Support")
    log.error.assert_not_called()


def test_invalid_message_is_logged_not_raised(caplog):
    db = MagicMock()
    bad = [{"toolType": "vectorSearch", "toolName": "s", "input": {}, "output": {}}]
    with caplog.at_level(logging.ERROR, logger=message_history.logger.name):
        result = store_ai_message(db, session_id=1, content="x", tool_calls=bad)
    assert result is not None  # still stored: validation is log-only
    db.add.assert_called_once()
    assert "Validation failed for schema 'chat_message' v2" in caplog.text
    assert "FileNotFoundError" not in caplog.text and "not found" not in caplog.text
