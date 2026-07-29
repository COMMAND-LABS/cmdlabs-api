"""Tests for the /api/accounts endpoints."""

import pytest
from httpx import AsyncClient


async def test_get_me_returns_account(authed_client: AsyncClient):
    response = await authed_client.get("/api/accounts/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "test@example.com"
    assert body["id"] == 1
    assert "newsletter_subscribed" in body
    # New accounts are free until Stripe says otherwise.
    assert body["role"] == "free"


@pytest.mark.parametrize("attempted_role", ["admin", "premium"])
async def test_role_is_not_self_updatable(
    authed_client: AsyncClient, attempted_role: str
):
    """Sending a role must not change it — role is not an updatable field."""
    response = await authed_client.put(
        "/api/accounts/me",
        json={"newsletter_subscribed": True, "role": attempted_role},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "free"


async def test_get_me_unauthenticated(client: AsyncClient):
    response = await client.get("/api/accounts/me")
    assert response.status_code == 401


async def test_get_me_with_cookie(client: AsyncClient, auth_token: str):
    client.cookies.set("jwt", auth_token)
    response = await client.get("/api/accounts/me")
    assert response.status_code == 200
    assert response.json()["email"] == "test@example.com"
