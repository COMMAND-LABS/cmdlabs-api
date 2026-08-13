"""Tests for credential encryption — especially fail-fast key handling.

Regression coverage for the footgun where a missing/invalid
CREDENTIALS_ENCRYPTION_KEY silently generated an ephemeral key, causing
credentials to become unrecoverable after a restart. The module must now fail
loudly instead.
"""

import pytest

from src.routers.credentials import encryption


def _reset_cipher_cache(monkeypatch, key_env):
    """Point the module at *key_env* and clear its cached ciphers (auto-restored)."""
    monkeypatch.setattr(encryption, "ENCRYPTION_KEY_ENV", key_env)
    monkeypatch.setattr(encryption, "_cached_keys", None)
    monkeypatch.setattr(encryption, "_fernet", None)
    monkeypatch.setattr(encryption, "_multi_fernet", None)


def test_valid_key_roundtrips():
    """With the (valid) test key from conftest, encrypt/decrypt round-trips."""
    payload = {"api_key": "sk-secret", "host": "db.example.com"}
    token = encryption.encrypt_credential_data(payload)
    assert isinstance(token, str)
    assert encryption.decrypt_credential_data(token) == payload


def test_missing_key_fails_fast(monkeypatch):
    """A missing key must raise, not silently generate an ephemeral key."""
    _reset_cipher_cache(monkeypatch, None)
    with pytest.raises(RuntimeError, match="CREDENTIALS_ENCRYPTION_KEY is not set"):
        encryption.get_encryption_keys()
    # And the failure must propagate through the public API.
    with pytest.raises(RuntimeError):
        encryption.encrypt_credential_data({"api_key": "x"})


def test_invalid_key_fails_fast(monkeypatch):
    """A malformed (non-Fernet) key must raise rather than fall back."""
    _reset_cipher_cache(monkeypatch, "not-a-valid-fernet-key")
    with pytest.raises(RuntimeError, match="not a valid Fernet key"):
        encryption.get_encryption_keys()


def test_no_silent_key_generation(monkeypatch):
    """Guard the regression directly: missing key never yields usable ciphers."""
    _reset_cipher_cache(monkeypatch, None)
    with pytest.raises(RuntimeError):
        encryption._ensure_ciphers_initialized()
    assert encryption._fernet is None
    assert encryption._multi_fernet is None
