"""Tests for the /api/billing endpoints.

The Stripe SDK is patched throughout — these cover our logic (who gets
entitled, what the webhook trusts), not Stripe's.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.db.models import Account, role_for_subscription


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,current_role,expected",
    [
        # An entitling subscription promotes a free account.
        ("active", "free", "premium"),
        ("trialing", "free", "premium"),
        # Anything else demotes back.
        ("past_due", "premium", "free"),
        ("canceled", "premium", "free"),
        ("unpaid", "premium", "free"),
        ("incomplete", "free", "free"),
        (None, "premium", "free"),
        # Staff are never moved in either direction.
        ("active", "admin", "admin"),
        ("canceled", "admin", "admin"),
        (None, "admin", "admin"),
        # Idempotent for accounts already in the right place.
        ("active", "premium", "premium"),
        (None, "free", "free"),
    ],
)
def test_role_for_subscription(status, current_role, expected):
    assert role_for_subscription(status, current_role) == expected

@pytest.mark.parametrize(
    "status,expected",
    [
        ("active", True),
        ("trialing", True),
        ("past_due", False),
        ("unpaid", False),
        ("incomplete", False),
        ("incomplete_expired", False),
        ("canceled", False),
        ("paused", False),
        (None, False),
    ],
)
def test_has_active_subscription(status, expected):
    """past_due/unpaid must not entitle: the card was never charged."""
    assert Account(email="x@example.com", subscription_status=status).has_active_subscription is expected


async def test_account_me_reports_subscription(authed_client: AsyncClient):
    response = await authed_client.get("/api/accounts/me")
    assert response.status_code == 200
    body = response.json()
    assert body["subscription_active"] is False
    assert body["subscription_status"] is None


async def test_account_me_reports_active_subscription(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    test_account.subscription_status = "active"
    db.flush()
    body = (await authed_client.get("/api/accounts/me")).json()
    assert body["subscription_active"] is True
    assert body["subscription_status"] == "active"


async def test_subscription_is_not_self_updatable(
    authed_client: AsyncClient, test_account: Account
):
    """Only the webhook grants membership — never the account holder."""
    response = await authed_client.put(
        "/api/accounts/me",
        json={"newsletter_subscribed": True, "subscription_status": "active"},
    )
    assert response.status_code == 200
    assert response.json()["subscription_active"] is False
    assert test_account.subscription_status is None


# ---------------------------------------------------------------------------
# Checkout session
# ---------------------------------------------------------------------------

async def test_checkout_session_requires_auth(client: AsyncClient):
    assert (await client.post("/api/billing/checkout-session")).status_code == 401


async def test_checkout_session_returns_stripe_url(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    test_account.stripe_customer_id = "cus_test123"
    db.flush()

    with patch(
        "src.routers.billing.checkout.create_subscription_checkout_session",
        return_value={"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"},
    ) as mock_create:
        response = await authed_client.post("/api/billing/checkout-session")

    assert response.status_code == 200
    assert response.json()["checkout_url"].startswith("https://checkout.stripe.com/")
    # The account id must ride along, or the webhook cannot find the account.
    assert mock_create.call_args.kwargs["account_id"] == test_account.id
    assert mock_create.call_args.kwargs["customer_id"] == "cus_test123"


async def test_checkout_session_rejects_existing_member(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    test_account.stripe_customer_id = "cus_test123"
    test_account.subscription_status = "active"
    db.flush()

    response = await authed_client.post("/api/billing/checkout-session")
    assert response.status_code == 409


def _missing_customer_error(customer_id: str = "cus_gone"):
    """The 400 Stripe returns for a customer id that does not resolve."""
    import stripe

    return stripe.error.InvalidRequestError(
        f"No such customer: '{customer_id}'",
        param="customer",
        code="resource_missing",
    )


async def test_checkout_recovers_from_a_missing_customer(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """
    A stored customer that no longer exists (deleted in the dashboard, or
    belonging to the other Stripe mode) must not brick the account forever.
    """
    test_account.stripe_customer_id = "cus_gone"
    db.flush()

    session = {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
    with patch(
        "src.routers.billing.checkout.create_subscription_checkout_session",
        side_effect=[_missing_customer_error(), session],
    ) as mock_create, patch(
        "src.routers.billing.checkout.create_stripe_customer",
        return_value="cus_fresh",
    ) as mock_customer:
        response = await authed_client.post("/api/billing/checkout-session")

    assert response.status_code == 200
    assert response.json()["checkout_url"] == session["url"]
    mock_customer.assert_called_once_with(test_account.email)
    # Retried with the replacement, and the replacement was persisted.
    assert mock_create.call_count == 2
    assert mock_create.call_args.kwargs["customer_id"] == "cus_fresh"
    assert test_account.stripe_customer_id == "cus_fresh"


async def test_checkout_does_not_retry_other_stripe_errors(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """Only a missing customer is recoverable — a bad price must surface."""
    import stripe

    test_account.stripe_customer_id = "cus_test123"
    db.flush()

    with patch(
        "src.routers.billing.checkout.create_subscription_checkout_session",
        side_effect=stripe.error.InvalidRequestError(
            "No such price: 'price_nope'", param="line_items[0][price]",
            code="resource_missing",
        ),
    ) as mock_create, patch(
        "src.routers.billing.checkout.create_stripe_customer",
    ) as mock_customer:
        response = await authed_client.post("/api/billing/checkout-session")

    assert response.status_code >= 400
    assert mock_create.call_count == 1
    mock_customer.assert_not_called()
    # The good customer id must survive an unrelated failure.
    assert test_account.stripe_customer_id == "cus_test123"


async def test_checkout_session_without_price_configured(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """A missing price id is a deployment fault, not a 500 in the buyer's face."""
    test_account.stripe_customer_id = "cus_test123"
    db.flush()

    with patch(
        "src.routers.billing.checkout.create_subscription_checkout_session",
        side_effect=ValueError("STRIPE_MEMBER_PRICE_ID is not configured"),
    ):
        response = await authed_client.post("/api/billing/checkout-session")

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Downgrade / resume
# ---------------------------------------------------------------------------

async def test_downgrade_schedules_cancellation_at_period_end(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """
    A downgrade must not revoke access immediately — they paid through the end
    of the cycle, so the role stays premium until Stripe actually ends it.
    """
    test_account.role = "premium"
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()

    with patch(
        "src.routers.billing.checkout.set_subscription_cancel_at_period_end",
        return_value={"id": "sub_test1", "cancel_at_period_end": True},
    ) as mock_cancel:
        response = await authed_client.post("/api/billing/downgrade")

    assert response.status_code == 200
    body = response.json()
    assert body["cancel_at_period_end"] is True
    assert body["active"] is True
    mock_cancel.assert_called_once_with("sub_test1", True)
    db.refresh(test_account)
    assert test_account.subscription_cancel_at_period_end is True
    # Still premium — the downgrade has been scheduled, not applied.
    assert test_account.role == "premium"
    assert test_account.has_active_subscription is True


async def test_resume_calls_off_a_scheduled_downgrade(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    test_account.role = "premium"
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    test_account.subscription_cancel_at_period_end = True
    db.flush()

    with patch(
        "src.routers.billing.checkout.set_subscription_cancel_at_period_end",
        return_value={"id": "sub_test1", "cancel_at_period_end": False},
    ) as mock_resume:
        response = await authed_client.post("/api/billing/resume")

    assert response.status_code == 200
    assert response.json()["cancel_at_period_end"] is False
    mock_resume.assert_called_once_with("sub_test1", False)
    db.refresh(test_account)
    assert test_account.subscription_cancel_at_period_end is False
    assert test_account.role == "premium"


async def test_downgrade_without_a_subscription_is_rejected(
    authed_client: AsyncClient, test_account: Account
):
    response = await authed_client.post("/api/billing/downgrade")
    assert response.status_code == 409


async def test_resume_without_a_subscription_is_rejected(
    authed_client: AsyncClient, test_account: Account
):
    response = await authed_client.post("/api/billing/resume")
    assert response.status_code == 409


async def test_downgrade_requires_auth(client: AsyncClient):
    assert (await client.post("/api/billing/downgrade")).status_code == 401


async def test_downgrade_then_period_end_demotes_to_free(
    client: AsyncClient, authed_client: AsyncClient, test_account: Account, db: Session
):
    """The full downgrade path: schedule it, then let the period actually end."""
    test_account.role = "premium"
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()

    with patch(
        "src.routers.billing.checkout.set_subscription_cancel_at_period_end",
        return_value={"id": "sub_test1", "cancel_at_period_end": True},
    ):
        await authed_client.post("/api/billing/downgrade")

    db.refresh(test_account)
    assert test_account.role == "premium"  # still paid up

    # Stripe ends it when the period runs out.
    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_subscription_event(
            "customer.subscription.deleted", test_account.id, "canceled"
        ),
    ):
        await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    db.refresh(test_account)
    assert test_account.role == "free"
    assert test_account.subscription_cancel_at_period_end is False


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------

def _checkout_completed_event(account_id: int, subscription_id: str = "sub_test1"):
    return {
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "mode": "subscription",
                "client_reference_id": str(account_id),
                "customer": "cus_test123",
                "subscription": subscription_id,
            }
        },
    }


def _subscription_event(event_type: str, account_id: int, status: str, sub_id="sub_test1"):
    return {
        "type": event_type,
        "data": {
            "object": {
                "id": sub_id,
                "customer": "cus_test123",
                "status": status,
                "current_period_end": 1893456000,  # 2030-01-01Z
                "metadata": {"account_id": str(account_id)},
            }
        },
    }


class _FakeSubscription(dict):
    """stripe.Subscription.retrieve() returns a dict-like object."""


async def test_webhook_rejects_bad_signature(client: AsyncClient):
    import stripe

    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        side_effect=stripe.error.SignatureVerificationError("bad sig", "sig_header"),
    ):
        response = await client.post(
            "/api/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=forged"},
        )

    assert response.status_code == 400


async def test_webhook_rejects_unverifiable_payload(client: AsyncClient):
    """No secret configured must fail closed, not grant a free membership."""
    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        side_effect=ValueError("STRIPE_WEBHOOK_SECRET is not configured"),
    ):
        response = await client.post("/api/billing/webhook", content=b"{}")

    assert response.status_code == 400


async def test_webhook_checkout_completed_grants_membership(
    client: AsyncClient, test_account: Account, db: Session
):
    subscription = _FakeSubscription(
        id="sub_test1", status="active", current_period_end=1893456000
    )

    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_checkout_completed_event(test_account.id),
    ), patch("stripe.Subscription.retrieve", return_value=subscription):
        response = await client.post(
            "/api/billing/webhook",
            content=b"{}",
            headers={"stripe-signature": "t=1,v1=valid"},
        )

    assert response.status_code == 200
    assert response.json()["handled"] is True
    db.refresh(test_account)
    assert test_account.subscription_status == "active"
    assert test_account.stripe_subscription_id == "sub_test1"
    assert test_account.has_active_subscription is True
    assert test_account.subscription_current_period_end == datetime(
        2030, 1, 1, tzinfo=timezone.utc
    )
    # Paying is what promotes free -> member.
    assert test_account.role == "premium"


async def test_webhook_never_demotes_an_admin(
    client: AsyncClient, test_account: Account, db: Session
):
    """Staff are not billed — a cancellation must not strip their access."""
    test_account.role = "admin"
    test_account.stripe_subscription_id = "sub_test1"
    db.flush()

    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_subscription_event(
            "customer.subscription.deleted", test_account.id, "canceled"
        ),
    ):
        await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    db.refresh(test_account)
    assert test_account.role == "admin"
    assert test_account.subscription_status == "canceled"


async def test_webhook_never_promotes_an_admin(
    client: AsyncClient, test_account: Account, db: Session
):
    test_account.role = "admin"
    db.flush()
    subscription = _FakeSubscription(id="sub_test1", status="active", current_period_end=None)

    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_checkout_completed_event(test_account.id),
    ), patch("stripe.Subscription.retrieve", return_value=subscription):
        await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    db.refresh(test_account)
    assert test_account.role == "admin"


async def test_webhook_subscription_deleted_revokes(
    client: AsyncClient, test_account: Account, db: Session
):
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()

    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_subscription_event(
            "customer.subscription.deleted", test_account.id, "canceled"
        ),
    ):
        response = await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    assert response.status_code == 200
    db.refresh(test_account)
    assert test_account.subscription_status == "canceled"
    assert test_account.has_active_subscription is False
    # Losing the subscription demotes on the same commit that records it.
    assert test_account.role == "free"


async def test_webhook_past_due_does_not_entitle(
    client: AsyncClient, test_account: Account, db: Session
):
    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value=_subscription_event(
            "customer.subscription.updated", test_account.id, "past_due"
        ),
    ):
        await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    db.refresh(test_account)
    assert test_account.subscription_status == "past_due"
    assert test_account.has_active_subscription is False
    assert test_account.role == "free"


async def test_webhook_resolves_account_by_subscription_id(
    client: AsyncClient, test_account: Account, db: Session
):
    """An event with no metadata still has to find its account."""
    test_account.stripe_subscription_id = "sub_test1"
    db.flush()

    event = _subscription_event("customer.subscription.updated", test_account.id, "active")
    event["data"]["object"]["metadata"] = {}

    with patch("src.routers.billing.webhook.construct_webhook_event", return_value=event):
        response = await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    assert response.json()["handled"] is True
    db.refresh(test_account)
    assert test_account.subscription_status == "active"


async def test_webhook_unknown_account_is_acknowledged(client: AsyncClient):
    """Retrying cannot conjure an account, so do not make Stripe redeliver."""
    event = _subscription_event("customer.subscription.updated", 999999, "active")
    event["data"]["object"]["customer"] = "cus_nobody"

    with patch("src.routers.billing.webhook.construct_webhook_event", return_value=event):
        response = await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    assert response.status_code == 200
    assert response.json()["handled"] is False


async def test_webhook_ignores_unrelated_events(client: AsyncClient):
    with patch(
        "src.routers.billing.webhook.construct_webhook_event",
        return_value={"type": "invoice.created", "data": {"object": {}}},
    ):
        response = await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    assert response.status_code == 200
    assert response.json()["handled"] is False


async def test_webhook_ignores_one_off_checkout(client: AsyncClient, test_account: Account):
    """A one-time payment is not a membership."""
    event = _checkout_completed_event(test_account.id)
    event["data"]["object"]["mode"] = "payment"

    with patch("src.routers.billing.webhook.construct_webhook_event", return_value=event):
        response = await client.post(
            "/api/billing/webhook", content=b"{}", headers={"stripe-signature": "ok"}
        )

    assert response.json()["handled"] is False
