"""Tests for message history building and storage."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from src.agent_runtime.helpers.message_history import (
    build_message_history,
    store_ai_message,
    store_user_message,
)


def test_build_empty_history():
    history = build_message_history([])
    assert len(history.messages) == 0


def test_build_history_from_db_messages():
    messages = [
        SimpleNamespace(message={"role": "human", "content": "hi"}),
        SimpleNamespace(message={"role": "ai", "content": "hello"}),
        SimpleNamespace(message={"role": "human", "content": "how are you?"}),
    ]
    history = build_message_history(messages)
    assert len(history.messages) == 3
    assert history.messages[0].content == "hi"
    assert history.messages[1].content == "hello"


def test_build_history_skips_malformed():
    messages = [
        SimpleNamespace(message={"role": "human", "content": "valid"}),
        SimpleNamespace(message={"broken": "data"}),
        SimpleNamespace(message="not a dict"),
    ]
    history = build_message_history(messages)
    assert len(history.messages) == 1


def test_store_user_message(mock_db):
    store_user_message(mock_db, session_id=1, prompt="hello", validate=False)
    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()


def test_store_ai_message_with_tool_calls(mock_db):
    calls = [{"toolType": "vectorSearch", "toolName": "search"}]
    store_ai_message(mock_db, session_id=1, content="here are the results", tool_calls=calls, validate=False)
    mock_db.add.assert_called_once()
    added = mock_db.add.call_args[0][0]
    assert added.message["toolCalls"] == calls


def test_store_user_message_with_pdf(mock_db):
    store_user_message(mock_db, session_id=1, prompt="analyze this", pdf_filename="doc.pdf", validate=False)
    added = mock_db.add.call_args[0][0]
    assert added.message["attachments"][0]["filename"] == "doc.pdf"


def test_store_handles_db_error():
    db = MagicMock()
    db.commit.side_effect = Exception("DB error")
    result = store_user_message(db, session_id=1, prompt="will fail", validate=False)
    assert result is None
    db.rollback.assert_called_once()
