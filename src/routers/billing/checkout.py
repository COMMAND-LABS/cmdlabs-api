"""
Stripe Checkout + billing-portal session endpoints.

Card details never touch this service: both endpoints hand back a Stripe-hosted
URL for the client to redirect to. Checkout brings Link / Apple Pay / Google
Pay, 3DS, receipts and promo codes with it, and keeps us in PCI SAQ A.
"""
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, status
import stripe

from src.config import plans_registry as plans
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.clients.stripe_client import (
    create_billing_portal_session,
    create_stripe_customer,
    create_subscription_checkout_session,
    cancel_subscription_now,
)
from src.utils.errors import handle_db_error
from src.rate_limit import limiter
from .webhook import apply_subscription_change
from .models import CheckoutSessionResponse, PortalSessionResponse, SubscriptionResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# Where Stripe returns the buyer. Built server-side from config rather than
# taken from the request body — a caller-supplied return URL would turn this
# endpoint into an open redirect with a Stripe-branded referrer.
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:3001")

# Both land on the Membership page rather than the dashboard root: Stripe can
# return the buyer before the webhook has been delivered, so the page that
# knows how to wait for the subscription to appear is the safe destination.
SUCCESS_PATH = "/dashboard/membership?checkout=success"
CANCEL_PATH = "/dashboard/membership?checkout=cancelled"
PORTAL_RETURN_PATH = "/dashboard/membership"


def _is_missing_customer(error: stripe.error.StripeError) -> bool:
    """
    Whether Stripe rejected the call because the customer id we hold no longer
    resolves — deleted in the dashboard, or belonging to the other mode after a
    test/live key swap.
    """
    code = getattr(error, "code", None) or getattr(getattr(error, "error", None), "code", None)
    param = getattr(error, "param", None) or getattr(getattr(error, "error", None), "param", None)
    return code == "resource_missing" and param == "customer"


@router.post("/checkout-session", response_model=CheckoutSessionResponse)
@limiter.limit("10/minute")
async def create_checkout_session(
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """
    Start a Member subscription for the authenticated account.

    The account already exists by the time this is called — signup completes
    first, checkout second — so an abandoned checkout still leaves a real
    account behind rather than losing the lead entirely.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    if account.has_active_subscription:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account already has an active membership",
        )

    # Every account gets a Stripe customer at signup, but that call is
    # best-effort there — create one now if it did not land.
    if not account.stripe_customer_id:
        try:
            account.stripe_customer_id = create_stripe_customer(account.email)
            db.commit()
            db.refresh(account)
        except stripe.error.StripeError as e:
            db.rollback()
            raise handle_db_error(e, "[STRIPE ERROR CREATING CUSTOMER]")

    def _open_session() -> dict:
        return create_subscription_checkout_session(
            customer_id=account.stripe_customer_id,
            account_id=account.id,
            success_url=f"{APP_BASE_URL}{SUCCESS_PATH}",
            cancel_url=f"{APP_BASE_URL}{CANCEL_PATH}",
        )

    try:
        try:
            session = _open_session()
        except stripe.error.StripeError as e:
            if not _is_missing_customer(e):
                raise
            # The stored customer does not exist in this Stripe account —
            # deleted in the dashboard, or created under the other mode's
            # key. Without this, that account could never subscribe again.
            # Safe to replace: a customer id we cannot resolve holds nothing
            # we could keep.
            logger.warning(
                "[BILLING] Customer %s missing for account %s — creating a replacement",
                account.stripe_customer_id, account.id,
            )
            account.stripe_customer_id = create_stripe_customer(account.email)
            db.commit()
            db.refresh(account)
            session = _open_session()
    except ValueError as e:
        # No price configured — a deployment problem, not the caller's fault.
        logger.error("[BILLING] %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Memberships are not available right now",
        )
    except stripe.error.StripeError as e:
        raise handle_db_error(e, "[STRIPE ERROR CREATING CHECKOUT SESSION]")

    logger.info("[BILLING] Checkout session %s opened for account %s", session["id"], account.id)
    return CheckoutSessionResponse(checkout_url=session["url"], session_id=session["id"])


@router.post("/portal-session", response_model=PortalSessionResponse)
@limiter.limit("10/minute")
async def create_portal_session(
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """
    Hand back a Stripe billing-portal URL so a member can change their card or
    cancel. Cancellation comes back to us as a webhook like any other change.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    if not account.stripe_customer_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No billing account to manage yet",
        )

    try:
        portal_url = create_billing_portal_session(
            account.stripe_customer_id,
            return_url=f"{APP_BASE_URL}{PORTAL_RETURN_PATH}",
        )
    except stripe.error.StripeError as e:
        raise handle_db_error(e, "[STRIPE ERROR CREATING PORTAL SESSION]")

    return PortalSessionResponse(portal_url=portal_url)


@router.get("/subscription", response_model=SubscriptionResponse)
@limiter.limit("30/minute")
async def get_subscription_status(
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """
    The authenticated account's subscription state, as last reported by Stripe.

    Read from our own columns rather than by calling Stripe: the webhook keeps
    them current, and entitlement checks should not depend on a live API call.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    period_end = account.subscription_current_period_end
    return SubscriptionResponse(
        status=account.subscription_status,
        active=account.has_active_subscription,
        current_period_end=period_end.isoformat() if period_end else None,
    )


@router.post("/downgrade", response_model=SubscriptionResponse)
@limiter.limit("10/minute")
async def downgrade_to_free(
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
):
    """
    Cancel the membership immediately.

    The account stops being paid in this request rather than waiting for a
    webhook, so a dropped or undelivered event can never leave someone on
    Premium for free. It is audited like any lapse (org.suspend) but, unlike a
    failed payment, gets no grace window. Stripe also emits
    customer.subscription.deleted; by then a lapse is on record, so applying it
    changes nothing — a safety net if the write below failed.

    Note: unused paid time is NOT refunded. Coming back is a fresh checkout.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    if not account.stripe_subscription_id or not account.has_active_subscription:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account does not have an active membership to cancel",
        )

    try:
        subscription = cancel_subscription_now(account.stripe_subscription_id)
    except stripe.error.StripeError as e:
        raise handle_db_error(e, "[STRIPE ERROR CANCELLING SUBSCRIPTION]")

    # Apply what Stripe reports through the same path as the subscription
    # webhooks, so the transition is audited (org.suspend) here and now.
    apply_subscription_change(
        db, account, {**subscription, "status": subscription.get("status") or "canceled"}
    )
    # A voluntary cancel gets no grace window — grace is a courtesy for a
    # failed payment, and cancelling means free now. Record the lapse as one
    # whose window has already closed: billing_state reads that as LAPSED, and
    # because a lapse is now on record, the customer.subscription.deleted
    # webhook that follows cannot stamp a fresh one and start a window late.
    account.subscription_lapsed_at = (
        datetime.now(timezone.utc) - timedelta(days=plans.GRACE_DAYS)
    )
    db.commit()
    db.refresh(account)

    logger.info(
        "[BILLING] Account %s cancelled subscription %s -> status %s",
        account.id, account.stripe_subscription_id,
        account.subscription_status,
    )
    period_end = account.subscription_current_period_end
    return SubscriptionResponse(
        status=account.subscription_status,
        active=account.has_active_subscription,
        current_period_end=period_end.isoformat() if period_end else None,
    )
