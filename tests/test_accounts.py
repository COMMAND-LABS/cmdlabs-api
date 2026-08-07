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
    assert body["is_staff"] is False


@pytest.mark.parametrize("attempted", [True, "yes"])
async def test_staff_is_not_self_updatable(
    authed_client: AsyncClient, attempted
):
    """Sending is_staff must not grant it. No API path makes somebody staff."""
    response = await authed_client.put(
        "/api/accounts/me",
        json={"newsletter_subscribed": True, "is_staff": attempted},
    )
    assert response.status_code == 200
    assert response.json()["is_staff"] is False


async def test_get_me_unauthenticated(client: AsyncClient):
    response = await client.get("/api/accounts/me")
    assert response.status_code == 401


async def test_get_me_with_cookie(client: AsyncClient, auth_token: str):
    client.cookies.set("jwt", auth_token)
    response = await client.get("/api/accounts/me")
    assert response.status_code == 200
    assert response.json()["email"] == "test@example.com"
