"""
Encryption utility for securely storing and retrieving credentials.
Uses Fernet symmetric encryption from the cryptography library.
Supports key rotation by maintaining multiple encryption keys.

Supports multiple credential types:
- API keys (string)
- Database connections (host, port, username, password, etc.)
- OAuth credentials (client_id, client_secret, tokens)
- SSH keys (private key, passphrase)
- Certificates (cert data, private key)
"""
import json
import logging
import os
from typing import Any

from cryptography.fernet import Fernet, MultiFernet

logger = logging.getLogger(__name__)

# Get encryption keys from environment variables
# CREDENTIALS_ENCRYPTION_KEY: Current/primary key (required)
# CREDENTIALS_ENCRYPTION_KEY_OLD: Previous key(s) for decryption (optional, comma-separated)
ENCRYPTION_KEY_ENV = os.getenv("CREDENTIALS_ENCRYPTION_KEY")
ENCRYPTION_KEY_OLD_ENV = os.getenv("CREDENTIALS_ENCRYPTION_KEY_OLD", "")

def get_encryption_keys() -> list[bytes]:
    """
    Get Fernet keys: ``[current_key, old_key1, ...]``. The first key encrypts;
    all keys are tried for decryption (key-rotation support).

    Fails fast if ``CREDENTIALS_ENCRYPTION_KEY`` is missing or malformed. We
    deliberately never fall back to a generated key: doing so would silently
    encrypt credentials under an ephemeral key that is lost on restart, making
    every credential written in that window permanently unrecoverable.
    """
    if not ENCRYPTION_KEY_ENV:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not set. Refusing to proceed — "
            "generating an ephemeral key would make stored credentials "
            "unrecoverable after a restart. Set CREDENTIALS_ENCRYPTION_KEY to a "
            "urlsafe-base64-encoded 32-byte Fernet key."
        )

    current_key = ENCRYPTION_KEY_ENV.encode()
    try:
        Fernet(current_key)
    except Exception as exc:
        raise RuntimeError(
            "CREDENTIALS_ENCRYPTION_KEY is not a valid Fernet key (expected a "
            "urlsafe-base64-encoded 32-byte key)."
        ) from exc

    keys = [current_key]

    # Optional previous keys, tried only for decryption during key rotation.
    if ENCRYPTION_KEY_OLD_ENV:
        old_keys = (k.strip() for k in ENCRYPTION_KEY_OLD_ENV.split(",") if k.strip())
        for old_key in old_keys:
            old_key_bytes = old_key.encode()
            try:
                Fernet(old_key_bytes)
            except Exception:
                logger.warning(
                    "Ignoring an invalid CREDENTIALS_ENCRYPTION_KEY_OLD entry "
                    "(not a valid Fernet key)."
                )
                continue
            keys.append(old_key_bytes)

    return keys

# Cache for encryption keys (to avoid regenerating on each call)
_cached_keys: list[bytes] | None = None

def _get_cached_keys() -> list[bytes]:
    """Get encryption keys, using cache if available."""
    global _cached_keys
    if _cached_keys is None:
        _cached_keys = get_encryption_keys()
    return _cached_keys

def _get_fernet_cipher() -> Fernet:
    """Get Fernet cipher for encryption (uses current key only)."""
    keys = _get_cached_keys()
    if not keys:
        raise ValueError("No encryption keys available")
    return Fernet(keys[0])

def _get_multi_fernet_cipher() -> MultiFernet:
    """Get MultiFernet cipher for decryption (tries all keys)."""
    keys = _get_cached_keys()
    if not keys:
        raise ValueError("No encryption keys available")

    # MultiFernet requires at least one key, and tries them in order
    fernets = [Fernet(key) for key in keys]
    return MultiFernet(fernets)

# Initialize ciphers (lazy initialization)
_fernet: Fernet | None = None
_multi_fernet: MultiFernet | None = None

def _ensure_ciphers_initialized():
    """Initialize the encryption/decryption ciphers once (lazy).

    Propagates the ``RuntimeError`` from :func:`get_encryption_keys` if the key
    is missing or invalid. We deliberately do NOT fall back to a generated key —
    that would silently lose every credential written before the next restart.
    """
    global _fernet, _multi_fernet
    if _fernet is None or _multi_fernet is None:
        _fernet = _get_fernet_cipher()
        _multi_fernet = _get_multi_fernet_cipher()

# =============================================================================
# NEW FLEXIBLE CREDENTIAL ENCRYPTION FUNCTIONS
# =============================================================================

def encrypt_credential_data(data: dict[str, Any]) -> str:
    """
    Encrypt credential data (any structure) using Fernet symmetric encryption.

    The data is serialized to JSON before encryption, allowing storage of
    complex credential structures like database connections, OAuth tokens, etc.

    Args:
        data: Dictionary containing credential information.
              For API keys: {"api_key": "sk-..."}
              For DB connections: {"host": "...", "port": 5432, "username": "...", "password": "...", "database": "..."}
              For OAuth: {"client_id": "...", "client_secret": "...", "access_token": "...", "refresh_token": "..."}

    Returns:
        The encrypted credential data as a base64-encoded string

    Raises:
        ValueError: If data is empty or cannot be serialized
    """
    if not data:
        raise ValueError("Credential data cannot be empty")

    _ensure_ciphers_initialized()

    try:
        # Serialize to JSON
        json_str = json.dumps(data, default=str)

        # Encrypt
        encrypted_bytes = _fernet.encrypt(json_str.encode())
        return encrypted_bytes.decode()
    except (TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"Failed to serialize credential data: {e!s}") from e


def decrypt_credential_data(encrypted_data: str) -> dict[str, Any]:
    """
    Decrypt credential data and return as dictionary.
    Tries multiple keys to support key rotation.

    Args:
        encrypted_data: The encrypted credential data as a base64-encoded string

    Returns:
        Dictionary containing decrypted credential information

    Raises:
        ValueError: If decryption fails or data is malformed
    """
    if not encrypted_data:
        raise ValueError("Encrypted data cannot be empty")

    _ensure_ciphers_initialized()

    try:
        # MultiFernet tries all keys in order until one succeeds
        decrypted_bytes = _multi_fernet.decrypt(encrypted_data.encode())
        json_str = decrypted_bytes.decode()
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse decrypted credential data as JSON: {e!s}") from e
    except Exception as e:
        raise ValueError(f"Failed to decrypt credential data: {e!s}") from e


def get_credential_value(credential, key: str = "api_key") -> str:
    """
    Get a specific value from a credential.

    Args:
        credential: Credential model instance
        key: The key to extract from the credential data (default: "api_key")

    Returns:
        The requested credential value

    Raises:
        ValueError: If the credential cannot be decrypted or key not found
    """
    if not credential.encrypted_data:
        raise ValueError("No encrypted credential data found")

    data = decrypt_credential_data(credential.encrypted_data)

    if key in data:
        return data[key]

    raise ValueError(f"Key '{key}' not found in credential data. Available keys: {list(data.keys())}")

