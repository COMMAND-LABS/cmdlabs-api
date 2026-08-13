"""Tests for auth token extraction helpers."""

from unittest.mock import MagicMock

from src.agent_runtime.helpers.auth import extract_auth_token


def _make_request(cookies=None, headers=None):
    request = MagicMock()
    request.cookies = cookies or {}
    request.headers = headers or {}
    return request


def test_extracts_jwt_from_cookie():
    request = _make_request(cookies={"jwt": "my-jwt-token"})
    auth = {"auth_type": "jwt"}
    assert extract_auth_token(request, auth) == "my-jwt-token"


def test_extracts_api_key_from_bearer():
    request = _make_request(headers={"Authorization": "Bearer kalygo_live_abc123"})
    auth = {"auth_type": "api_key"}
    assert extract_auth_token(request, auth) == "kalygo_live_abc123"


def test_extracts_api_key_from_x_api_key():
    request = _make_request(headers={"X-API-Key": "kalygo_live_xyz"})
    auth = {"auth_type": "api_key"}
    assert extract_auth_token(request, auth) == "kalygo_live_xyz"


def test_returns_none_for_no_request():
    assert extract_auth_token(None, {"auth_type": "jwt"}) is None


def test_returns_none_for_missing_token():
    request = _make_request()
    assert extract_auth_token(request, {"auth_type": "jwt"}) is None
