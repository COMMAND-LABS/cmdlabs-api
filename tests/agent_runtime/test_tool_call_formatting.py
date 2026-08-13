"""Tests for tool call output formatting."""

from src.agent_runtime.helpers.tool_calls import format_tool_call


def test_vector_search_formats_correctly():
    result = format_tool_call(
        tool_name="vector_search",
        tool_input={"query": "what is Kalygo?", "top_k": 5},
        tool_output={"results": [{"id": "1", "score": 0.95, "metadata": {"text": "..."}}], "namespace": "docs", "index": "main"},
    )
    assert result["toolType"] == "vectorSearch"
    assert result["input"]["query"] == "what is Kalygo?"
    assert result["input"]["topK"] == 5
    assert len(result["output"]["results"]) == 1


def test_vector_search_rerank_uses_correct_type():
    result = format_tool_call(
        tool_name="vector_search_with_reranking",
        tool_input={"query": "test"},
        tool_output={"results": [], "namespace": "ns", "index": "idx"},
    )
    assert result["toolType"] == "vectorSearchWithReranking"


def test_vector_search_error_returns_none():
    result = format_tool_call(
        tool_name="vector_search",
        tool_input={"query": "test"},
        tool_output={"error": "connection failed"},
    )
    assert result is None


def test_db_table_read_formatting():
    result = format_tool_call(
        tool_name="db_table_read_users",
        tool_input={"filters": {"name": "Alice"}, "limit": 10, "offset": 0},
        tool_output={"results": [{"data": {"name": "Alice"}}], "table": "users", "count": 1},
    )
    assert result["toolType"] == "dbTableRead"
    assert result["output"]["table"] == "users"


def test_db_table_write_formatting():
    result = format_tool_call(
        tool_name="db_table_write_leads",
        tool_input={"name": "Bob", "email": "bob@test.com"},
        tool_output={"success": True, "table": "leads", "inserted": {"id": 1}, "message": "ok"},
    )
    assert result["toolType"] == "dbTableWrite"
    assert result["output"]["success"] is True


def test_send_email_formatting():
    result = format_tool_call(
        tool_name="send_txt_email_with_ses",
        tool_input={"to_email": "a@b.com", "subject": "Hi", "body": "Hello"},
        tool_output={"success": True, "message_id": "msg-123"},
    )
    assert result["toolType"] == "sendTxtEmailWithSes"
    assert result["output"]["messageId"] == "msg-123"


def test_html_email_formatting():
    result = format_tool_call(
        tool_name="send_html_email_with_ses",
        tool_input={"to_email": "a@b.com", "subject": "Hi", "template_id": 5, "variables": {"name": "Al"}},
        tool_output={"success": True, "message_id": "msg-456"},
    )
    assert result["toolType"] == "sendHtmlEmailWithSes"
    assert result["input"]["template_id"] == 5


def test_generic_tool_formatting():
    result = format_tool_call(
        tool_name="custom_tool_xyz",
        tool_input={"foo": "bar"},
        tool_output={"baz": 42},
    )
    assert result["toolType"] == "custom"
    assert result["toolName"] == "custom_tool_xyz"


def test_string_tool_output_gets_parsed():
    result = format_tool_call(
        tool_name="custom_tool",
        tool_input={},
        tool_output='{"key": "value"}',
    )
    assert result["output"] == {"key": "value"}


def test_repr_string_tool_output_gets_parsed():
    result = format_tool_call(
        tool_name="custom_tool",
        tool_input={},
        tool_output="{'key': 'value'}",
    )
    assert result["output"] == {"key": "value"}


def test_unparseable_string_wrapped():
    result = format_tool_call(
        tool_name="custom_tool",
        tool_input={},
        tool_output="just some plain text",
    )
    assert result["output"] == {"result": "just some plain text"}
