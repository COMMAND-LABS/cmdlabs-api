"""
LLM factory for creating language model instances based on agent config.

Supports:
- OpenAI (gpt-4o-mini, gpt-4o, etc.)
- Anthropic (claude-3-5-sonnet, claude-3-5-haiku, etc.)
- Google (gemini-2.0-flash, gemini-1.5-pro, etc.)
- Kimi (kimi-k2.6)
- Ollama (llama3.2, mistral, etc.)
"""
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

DEFAULT_MODEL_CONFIG = {
    "provider": "openai",
    "model": "gpt-4o-mini",
}

# Kimi (Moonshot) serves the OpenAI wire format, so ChatOpenAI drives it with a
# custom base_url rather than a dedicated integration package.
# https://platform.kimi.ai/docs/guide/kimi-k2-6-quickstart
KIMI_BASE_URL = "https://api.moonshot.ai/v1"

# K2.6 rejects any temperature or top_p other than its own fixed pair, so these
# are sent verbatim and the caller's temperature is ignored (see _create_kimi_llm).
KIMI_TEMPERATURE = 0.6
KIMI_TOP_P = 0.95


def get_model_config(agent_config: dict[str, Any]) -> dict[str, str]:
    """
    Extract model configuration from a v4 agent config.
    Returns the configured model if present, otherwise the default.
    """
    config_data = agent_config.get('data', {})
    model_config = config_data.get('model')
    if model_config:
        resolved = {
            "provider": model_config.get('provider', 'openai'),
            "model": model_config.get('model', 'gpt-4o-mini'),
        }
        # Optional explicit credential binding for turn completions. When present
        # it pins the exact credential (no drift); when absent the funding
        # account's default for the provider type is resolved at runtime.
        if model_config.get('credentialId') is not None:
            resolved["credentialId"] = model_config["credentialId"]
        return resolved
    return DEFAULT_MODEL_CONFIG.copy()


def create_llm(
    model_config: dict[str, str],
    credentials: dict[str, str],
    temperature: float = 0,
) -> tuple[BaseChatModel, str]:
    """
    Create a streaming LangChain LLM instance based on model configuration.

    Args:
        model_config: Dict with 'provider' and 'model' keys
        credentials: Dict mapping provider names to API keys
                    e.g., {'openai': 'sk-...', 'anthropic': 'sk-ant-...', 'kimi': 'sk-...'}
        temperature: Model temperature (0-1)

    Returns:
        Tuple of (LLM instance, provider name)

    Raises:
        ValueError: If provider is not supported or credentials are missing
    """
    provider = model_config.get('provider', 'openai')
    model = model_config.get('model', 'gpt-4o-mini')

    if provider == 'openai':
        return _create_openai_llm(model, credentials, temperature), provider
    if provider == 'anthropic':
        return _create_anthropic_llm(model, credentials, temperature), provider
    if provider == 'google':
        return _create_google_llm(model, credentials, temperature), provider
    if provider == 'kimi':
        return _create_kimi_llm(model, credentials), provider
    if provider == 'ollama':
        return _create_ollama_llm(model, temperature), provider
    raise ValueError(f"Unsupported LLM provider: {provider}")


def _create_openai_llm(
    model: str,
    credentials: dict[str, str],
    temperature: float,
) -> BaseChatModel:
    """Create OpenAI LLM instance."""
    from langchain_openai import ChatOpenAI

    api_key = credentials.get('openai')
    if not api_key:
        raise ValueError("OpenAI API key not found. Please add your OpenAI API key in account settings.")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        streaming=True,
        temperature=temperature,
        stream_usage=True,
        model_kwargs={"parallel_tool_calls": False},
    )


def _create_anthropic_llm(
    model: str,
    credentials: dict[str, str],
    temperature: float,
) -> BaseChatModel:
    """Create Anthropic LLM instance."""
    from langchain_anthropic import ChatAnthropic

    api_key = credentials.get('anthropic')
    if not api_key:
        raise ValueError("Anthropic API key not found. Please add your Anthropic API key in account settings.")

    return ChatAnthropic(
        model=model,
        api_key=api_key,
        streaming=True,
        temperature=temperature,
    )


def _create_google_llm(
    model: str,
    credentials: dict[str, str],
    temperature: float,
) -> BaseChatModel:
    """Create Google Gemini LLM instance."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    api_key = credentials.get('google')
    if not api_key:
        raise ValueError("Google Gemini API key not found. Please add your Google Gemini API key in account settings.")

    return ChatGoogleGenerativeAI(
        model=model,
        google_api_key=api_key,
        streaming=True,
        temperature=temperature,
    )


def _create_kimi_llm(
    model: str,
    credentials: dict[str, str],
) -> BaseChatModel:
    """
    Create Kimi (Moonshot) LLM instance over the OpenAI-compatible endpoint.

    Deliberately takes no temperature: K2.6 pins temperature and top_p to fixed
    values and errors on anything else, so the caller's setting cannot be honored.

    Thinking mode is disabled. It is the API default, but agents here run
    multi-step tool calls, and with thinking enabled Kimi requires the prior
    turn's `reasoning_content` to be echoed back on every follow-up request —
    which the LangChain OpenAI adapter does not round-trip. Leaving it on would
    fail the second tool-calling turn rather than the first.
    """
    from langchain_openai import ChatOpenAI

    api_key = credentials.get('kimi')
    if not api_key:
        raise ValueError("Kimi API key not found. Please add your Kimi API key in account settings.")

    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=KIMI_BASE_URL,
        streaming=True,
        temperature=KIMI_TEMPERATURE,
        top_p=KIMI_TOP_P,
        stream_usage=True,
        # `thinking` is a Kimi extension, not an OpenAI parameter. It has to go
        # through extra_body: the OpenAI SDK validates its own kwargs and raises
        # on unknown ones rather than forwarding them, so model_kwargs would fail
        # every request with "unexpected keyword argument 'thinking'".
        extra_body={"thinking": {"type": "disabled"}},
    )


def _create_ollama_llm(
    model: str,
    temperature: float,
) -> BaseChatModel:
    """Create Ollama LLM instance."""
    import os

    from langchain_ollama import ChatOllama

    # Ollama base URL can be configured via environment variable
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    return ChatOllama(
        model=model,
        base_url=base_url,
        streaming=True,
        temperature=temperature,
    )


def get_required_credential_type(provider: str) -> str | None:
    """
    Get the ServiceName credential type required for a provider.

    Args:
        provider: The LLM provider name

    Returns:
        ServiceName enum value, or None if no credential needed
    """
    from src.db.service_name import ServiceName

    provider_to_credential = {
        'openai': ServiceName.OPENAI_API_KEY,
        'anthropic': ServiceName.ANTHROPIC_API_KEY,
        'google': ServiceName.GOOGLE_GEMINI_API_KEY,
        'kimi': ServiceName.KIMI_API_KEY,
        'ollama': None,  # Ollama is self-hosted, no API key needed
    }

    return provider_to_credential.get(provider)
