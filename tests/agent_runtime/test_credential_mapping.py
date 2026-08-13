"""Tests for LLM provider credential mapping."""

from src.db.service_name import ServiceName
from src.agent_runtime.helpers.llm_factory import (
    DEFAULT_MODEL_CONFIG,
    get_model_config,
    get_required_credential_type,
)


def test_default_model_config():
    config = get_model_config({})
    assert config == DEFAULT_MODEL_CONFIG


def test_explicit_model_config():
    config = get_model_config({
        "data": {
            "model": {"provider": "anthropic", "model": "claude-3-5-sonnet"}
        }
    })
    assert config["provider"] == "anthropic"
    assert config["model"] == "claude-3-5-sonnet"


def test_openai_requires_credential():
    assert get_required_credential_type("openai") == ServiceName.OPENAI_API_KEY


def test_anthropic_requires_credential():
    assert get_required_credential_type("anthropic") == ServiceName.ANTHROPIC_API_KEY


def test_google_requires_credential():
    assert get_required_credential_type("google") == ServiceName.GOOGLE_GEMINI_API_KEY


def test_ollama_requires_no_credential():
    assert get_required_credential_type("ollama") is None


def test_unknown_provider_returns_none():
    assert get_required_credential_type("unknown_provider") is None
