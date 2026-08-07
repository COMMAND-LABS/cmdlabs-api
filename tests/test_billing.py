"""Tests for the /api/billing endpoints.

The Stripe SDK is patched throughout — these cover our logic (who gets
entitled, what the webhook trusts), not Stripe's.
"""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from httpx import AsyncClient
from sqlalchemy.orm import Session

from src.config import plans_registry as plans
from src.db.models import Account


# ---------------------------------------------------------------------------
# Entitlement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "status,expected",
    [
        # An entitling subscription is the premium plan.
        ("active", "premium"),
        ("trialing", "premium"),
        # Anything else is not.
        ("past_due", "free"),
        ("canceled", "free"),
        ("unpaid", "free"),
        ("incomplete", "free"),
        (None, "free"),
    ],
)
def test_plan_for_status_with_nothing_on_record(status, expected):
    """The entitlement rule with no lapse timestamp — i.e. no grace.

    A NULL subscription_lapsed_at means the platform has no instant to date a
    grace window from, so a non-entitling status drops straight to free. That
    is the honest answer for an account that never subscribed, and for one that
    lapsed before the column existed.

    Grace is exercised in test_grace_window.py, which is where the interesting
    half lives.
    """
    assert plans.plan_for(status, None) == expected

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

async def test_downgrade_cancels_immediately(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """
    Cancelling demotes in this request, not on a later webhook — a dropped
    event must never leave someone on Premium for free.
    """
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()
    assert plans.plan_for_account(test_account) == "premium"

    with patch(
        "src.routers.billing.checkout.cancel_subscription_now",
        return_value={"id": "sub_test1", "status": "canceled"},
    ) as mock_cancel:
        response = await authed_client.post("/api/billing/downgrade")

    assert response.status_code == 200
    body = response.json()
    assert body["active"] is False
    assert body["status"] == "canceled"
    mock_cancel.assert_called_once_with("sub_test1")
    db.refresh(test_account)
    assert plans.plan_for_account(test_account) == "free"
    assert test_account.has_active_subscription is False


async def test_downgrade_never_demotes_super_admin(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """Super admins keep the platform surface after a cancellation."""
    test_account.is_super_admin = True
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()

    with patch(
        "src.routers.billing.checkout.cancel_subscription_now",
        return_value={"id": "sub_test1", "status": "canceled"},
    ):
        response = await authed_client.post("/api/billing/downgrade")

    assert response.status_code == 200
    db.refresh(test_account)
    assert test_account.is_super_admin is True


async def test_downgrade_then_resubscribe(
    authed_client: AsyncClient, test_account: Account, db: Session
):
    """After cancelling, checkout must be available again straight away."""
    test_account.stripe_customer_id = "cus_test123"
    test_account.stripe_subscription_id = "sub_test1"
    test_account.subscription_status = "active"
    db.flush()

    # While subscribed, checkout is refused.
    assert (await authed_client.post("/api/billing/checkout-session")).status_code == 409

    with patch(
        "src.routers.billing.checkout.cancel_subscription_now",
        return_value={"id": "sub_test1", "status": "canceled"},
    ):
        await authed_client.post("/api/billing/downgrade")

    with patch(
        "src.routers.billing.checkout.create_subscription_checkout_session",
        return_value={"id": "cs_2", "url": "https://checkout.stripe.com/c/pay/cs_2"},
    ):
        again = await authed_client.post("/api/billing/checkout-session")
    assert again.status_code == 200


async def test_downgrade_without_a_subscription_is_rejected(
    authed_client: AsyncClient, test_account: Account
):
    response = await authed_client.post("/api/billing/downgrade")
    assert response.status_code == 409


async def test_downgrade_requires_auth(client: AsyncClient):
    assert (await client.post("/api/billing/downgrade")).status_code == 401


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
    # Paying is what puts them on the premium plan — derived, not stored.
    assert plans.plan_for_account(test_account) == "premium"


async def test_webhook_never_demotes_super_admin(
    client: AsyncClient, test_account: Account, db: Session
):
    """Super admins are not billed; a cancellation cannot strip access."""
    test_account.is_super_admin = True
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
    assert test_account.is_super_admin is True
    assert test_account.subscription_status == "canceled"


async def test_webhook_leaves_super_admin_alone(
    client: AsyncClient, test_account: Account, db: Session
):
    test_account.is_super_admin = True
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
    assert test_account.is_super_admin is True


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
    # The lapse is DATED, and that is the whole record of it. Everything
    # downstream — read-only now, free plan in a fortnight — is a comparison
    # against this instant.
    assert test_account.subscription_lapsed_at is not None
    # Still premium, because they are inside the grace window: the modules stay
    # so their data stays visible, and deps refuses the writes.
    assert plans.plan_for_account(test_account) == "premium"
    assert plans.billing_state(
        test_account.subscription_status,
        test_account.subscription_lapsed_at) == plans.BILLING_GRACE


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
    # past_due is a lapse like any other: it opens the grace window rather than
    # demoting on the spot. A card that failed once is the case this exists for.
    assert test_account.subscription_lapsed_at is not None
    assert plans.plan_for_account(test_account) == "premium"


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
