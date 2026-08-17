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
    assert body["is_super_admin"] is False


@pytest.mark.parametrize("attempted", [True, "yes"])
async def test_super_admin_is_not_self_updatable(
    authed_client: AsyncClient, attempted
):
    """Sending is_super_admin must not grant it. No API path confers it."""
    response = await authed_client.put(
        "/api/accounts/me",
        json={"newsletter_subscribed": True, "is_super_admin": attempted},
    )
    assert response.status_code == 200
    assert response.json()["is_super_admin"] is False


async def test_name_starts_null_and_is_updatable(authed_client: AsyncClient):
    response = await authed_client.get("/api/accounts/me")
    assert response.json()["name"] is None

    # A name-only update satisfies the at-least-one-field check.
    response = await authed_client.put(
        "/api/accounts/me", json={"name": "  Tad Example  "}
    )
    assert response.status_code == 200
    assert response.json()["name"] == "Tad Example"

    response = await authed_client.get("/api/accounts/me")
    assert response.json()["name"] == "Tad Example"


async def test_name_clears_back_to_null(authed_client: AsyncClient):
    """Whitespace-only clears; an omitted field leaves the name alone."""
    await authed_client.put("/api/accounts/me", json={"name": "Tad"})

    response = await authed_client.put(
        "/api/accounts/me", json={"newsletter_subscribed": True}
    )
    assert response.json()["name"] == "Tad"

    response = await authed_client.put("/api/accounts/me", json={"name": "   "})
    assert response.status_code == 200
    assert response.json()["name"] is None


async def test_get_me_unauthenticated(client: AsyncClient):
    response = await client.get("/api/accounts/me")
    assert response.status_code == 401


async def test_get_me_with_cookie(client: AsyncClient, auth_token: str):
    client.cookies.set("jwt", auth_token)
    response = await client.get("/api/accounts/me")
    assert response.status_code == 200
    assert response.json()["email"] == "test@example.com"
