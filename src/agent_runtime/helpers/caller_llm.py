"""
The caller's own provider credential -> a ready LLM.

Shared by the endpoints that have no agent (and so no other principal whose
key could apply): llm-chat, memory-chat and pdf-to-faq. Each of them resolves
the CALLER's default credential for the chosen provider, decrypts it and
builds the LLM. The agent runtime (context.py) does NOT use this — it also
handles pinned credentials and owner-funded runs.

Failures raise one of the LlmSetupError subclasses below; each caller maps
them to its own wire shape (an SSE error frame, or an HTTPException). The
exception message is the underlying error's text, so ``str(exc)`` is exactly
what the callers used to surface.
"""
from langchain_core.language_models.chat_models import BaseChatModel

from src.routers.credentials.encryption import get_credential_value
from src.services.credential_access import resolve_default_credential

from .llm_factory import create_llm, get_required_credential_type


class LlmSetupError(Exception):
    """Base class for caller-LLM setup failures."""


class MissingCredentialError(LlmSetupError):
    """The caller has no credential for a provider that requires one."""


class CredentialDecryptError(LlmSetupError):
    """The caller's credential could not be decrypted/read."""


class LlmInitError(LlmSetupError):
    """create_llm rejected the model config (raised ValueError)."""


def create_caller_llm(
    db,
    account_id: int,
    provider: str,
    model: str,
    temperature: float = 0,
) -> BaseChatModel:
    """Build the LLM for ``provider``/``model`` on the caller's own key.

    Raises:
        MissingCredentialError: no default credential for the provider.
        CredentialDecryptError: the credential's api_key could not be read
            (chained from the original exception).
        LlmInitError: create_llm raised ValueError (chained from it).
    """
    credentials: dict[str, str] = {}
    required_credential_type = get_required_credential_type(provider)
    if required_credential_type:
        credential = resolve_default_credential(
            db, account_id, required_credential_type)
        if not credential:
            raise MissingCredentialError(provider)
        try:
            credentials[provider] = get_credential_value(
                credential, "api_key")
        except Exception as exc:
            raise CredentialDecryptError(str(exc)) from exc

    try:
        return create_llm(
            model_config={"provider": provider, "model": model},
            credentials=credentials,
            temperature=temperature,
        )
    except ValueError as exc:
        raise LlmInitError(str(exc)) from exc
