"""Shared test fixtures.

These fixtures provide lightweight mocks for the DB, auth, and FastAPI test
client so tests run instantly without touching real infrastructure.
"""

import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

# Ensure env vars are set before any app module is imported
# FORCE-SET, exactly as cmdlabs-api/tests/conftest.py does, and for the same
# two reasons.
#
# 1. Safety. The container inherits POSTGRES_URL pointing at PRODUCTION. With
#    setdefault that value survived, and the suite's own prod-host guard then
#    (correctly) skipped every database test. Overwriting means the tests can
#    never reach production even by accident.
#
# 2. Coverage. That skip was silent: 14 tests, including every tool-level
#    isolation check, reported green while never executing. A skipped security
#    test looks identical to a passing one, which is the worst failure mode
#    this particular suite could have.
#
# Service name, not localhost — these run inside the container, where
# localhost is the container itself.
os.environ["POSTGRES_URL"] = os.environ.get(
    "POSTGRES_TEST_URL", "postgresql://test:test@cmdlabs-test-pg:5432/kalygo_test"
)
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-for-tests")
os.environ.setdefault("AUTH_ALGORITHM", "HS256")
os.environ.setdefault("CREDENTIALS_ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcyEhISE=")
os.environ.setdefault("EMBEDDINGS_API_URL", "http://localhost:9100")
os.environ.setdefault("RERANKER_API_URL", "http://localhost:7100")


@pytest.fixture
def mock_db():
    """A mock SQLAlchemy session."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
    return db


@pytest.fixture
def fake_auth():
    """Auth dict matching what deps.get_current_user_or_api_key returns."""
    return {"email": "test@example.com", "id": 1, "auth_type": "jwt"}


@pytest.fixture
def fake_agent():
    """A minimal Agent-like object for unit tests."""
    return SimpleNamespace(
        id=42,
        account_id=1,
        name="Test Agent",
        config={
            "data": {
                "systemPrompt": "You are a helpful assistant.",
                "model": {"provider": "openai", "model": "gpt-4o-mini"},
                "tools": [],
            }
        },
    )


@pytest.fixture
def test_client():
    """FastAPI TestClient with auth, DB, and org-context deps overridden.

    The org-context override is new since the merge: routes are mounted
    through main.py's module-gated registry, and get_org_context needs real
    account/membership rows this mock DB does not provide. A super-admin
    context bypasses module gating only (never row scoping), which is exactly
    the axis these endpoint tests don't exercise.
    """
    from src.deps import get_current_user_or_api_key, get_db, get_org_context
    from src.main import app

    mock_db = MagicMock()

    def override_db():
        yield mock_db

    def override_auth():
        return {"email": "test@example.com", "id": 1, "auth_type": "jwt"}

    def override_org_context():
        return SimpleNamespace(
            account_id=1,
            org_id=1,
            role="manager",
            is_super_admin=True,
            is_owner=True,
            is_read_only=False,
        )

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_current_user_or_api_key] = override_auth
    app.dependency_overrides[get_org_context] = override_org_context

    client = TestClient(app)
    yield client, mock_db

    app.dependency_overrides.clear()
