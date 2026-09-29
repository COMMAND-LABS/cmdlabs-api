"""Reranking is a vectorSearch setting; vectorSearchWithReranking is its alias.

Both build the same tool: a config written either way searches, names and
reranks identically, so saved agents keep working after the type was merged.
"""
from unittest.mock import MagicMock

import pytest
from jsonschema import ValidationError

from src.agent_runtime.tools import vector_search as vector_search_mod
from src.agent_runtime.tools.registry import ToolRegistry
from src.schemas import validate_against_schema

BASE = {"provider": "pinecone", "index": "idx", "namespace": "ns"}


@pytest.fixture(autouse=True)
def fake_pinecone(monkeypatch):
    monkeypatch.setattr(vector_search_mod, "can_read_vector_store", lambda *a, **k: True)
    monkeypatch.setattr(vector_search_mod, "load_pinecone_index",
                        lambda tool_config, account_id, db, **kw: (MagicMock(), "ns", "idx"))


async def _build(config):
    builder = ToolRegistry.get_builder(config["type"])
    return await builder(tool_config=config, account_id=1, db=None)


async def test_plain_search_by_default():
    tool = await _build({"type": "vectorSearch", **BASE})
    assert tool.name == "vector_search"
    assert "top_n" not in tool.args_schema.model_fields


async def test_the_rerank_flag_and_the_legacy_type_build_the_same_tool():
    flagged = await _build({"type": "vectorSearch", "rerank": True, "topN": 3, **BASE})
    legacy = await _build({"type": "vectorSearchWithReranking", "topN": 3, **BASE})
    for tool in (flagged, legacy):
        # Same default name as before the merge, so prompts that mention it still work.
        assert tool.name == "vector_search_with_reranking"
        assert tool.args_schema.model_fields["top_k"].default == 20
        assert tool.args_schema.model_fields["top_n"].default == 3


async def test_a_configured_name_wins():
    tool = await _build({"type": "vectorSearch", "name": "search_sops", "rerank": True, **BASE})
    assert tool.name == "search_sops"


def _config(tool):
    return {"schema": "agent_config", "version": 4,
            "data": {"systemPrompt": "hi", "tools": [tool]}}


def test_schema_accepts_rerank_top_n_and_name():
    validate_against_schema(_config({"type": "vectorSearch", "name": "search_sops",
                                     "rerank": True, "topK": 20, "topN": 5, **BASE}),
                            "agent_config", 4)
    # Saved configs using the old type still validate.
    validate_against_schema(_config({"type": "vectorSearchWithReranking", "topN": 5, **BASE}),
                            "agent_config", 4)


def test_schema_rejects_a_name_the_model_cannot_call():
    with pytest.raises(ValidationError):
        validate_against_schema(_config({"type": "vectorSearch", "name": "Search SOPs!", **BASE}),
                                "agent_config", 4)
