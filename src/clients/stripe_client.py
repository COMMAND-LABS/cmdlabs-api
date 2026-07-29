import logging
import stripe
import os

logger = logging.getLogger(__name__)

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def create_stripe_customer(email: str) -> str:
    """
    Create a Stripe customer and return the customer ID.
    
    Args:
        email: The email address for the customer
        
    Returns:
        The Stripe customer ID (e.g., 'cus_xxxxx')
        
    Raises:
        stripe.error.StripeError: If Stripe API call fails
    """
    try:
        customer = stripe.Customer.create(email=email)
        return customer.id
    except stripe.error.StripeError:
        logger.exception("Stripe error creating customer for %s", email)
        raise


# ---------------------------------------------------------------------------
# Checkout + subscriptions
#
# Card details never reach this server: Checkout is hosted by Stripe, so the
# API only ever handles ids. That is what keeps this service in PCI SAQ A.
# ---------------------------------------------------------------------------

MEMBER_PRICE_ID = os.getenv("STRIPE_MEMBER_PRICE_ID")
WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")


def create_subscription_checkout_session(
    customer_id: str,
    account_id: int,
    success_url: str,
    cancel_url: str,
    price_id: str = None,
) -> dict:
    """
    Open a Stripe Checkout session in subscription mode for one account.

    Args:
        customer_id: The account's Stripe customer ('cus_xxxxx'), so the
            subscription attaches to the customer we already created at signup
        account_id: Our account id, echoed back on the webhook as
            client_reference_id — this is how the webhook finds the account
        success_url / cancel_url: Where Stripe returns the buyer
        price_id: Recurring price to bill; defaults to STRIPE_MEMBER_PRICE_ID

    Returns:
        {"id": <session id>, "url": <hosted checkout url>}

    Raises:
        ValueError: If no price id is configured
        stripe.error.StripeError: If the Stripe API call fails
    """
    price = price_id or MEMBER_PRICE_ID
    if not price:
        raise ValueError("STRIPE_MEMBER_PRICE_ID is not configured")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            customer=customer_id,
            line_items=[{"price": price, "quantity": 1}],
            # Echoed back on checkout.session.completed. Carried in both places
            # so the webhook can resolve the account from the session *or* from
            # the subscription it creates.
            client_reference_id=str(account_id),
            subscription_data={"metadata": {"account_id": str(account_id)}},
            success_url=success_url,
            cancel_url=cancel_url,
            allow_promotion_codes=True,
        )
        return {"id": session.id, "url": session.url}
    except stripe.error.StripeError:
        logger.exception("Stripe error creating checkout session for account %s", account_id)
        raise


def construct_webhook_event(payload: bytes, signature_header: str):
    """
    Verify a webhook payload against STRIPE_WEBHOOK_SECRET and return the event.

    Verification is mandatory: the webhook endpoint is unauthenticated and
    grants paid access, so an unverified payload is an open door to free
    memberships.

    Raises:
        ValueError: If the secret is missing or the payload is malformed
        stripe.error.SignatureVerificationError: If the signature does not match
    """
    if not WEBHOOK_SECRET:
        raise ValueError("STRIPE_WEBHOOK_SECRET is not configured")
    return stripe.Webhook.construct_event(payload, signature_header, WEBHOOK_SECRET)


def get_subscription(subscription_id: str):
    """Retrieve one subscription. Raises stripe.error.StripeError on failure."""
    try:
        return stripe.Subscription.retrieve(subscription_id)
    except stripe.error.StripeError:
        logger.exception("Stripe error retrieving subscription %s", subscription_id)
        raise


def create_billing_portal_session(customer_id: str, return_url: str) -> str:
    """
    Open a Stripe billing-portal session so a member can update their card or
    cancel without any subscription-management UI existing here.
    """
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=return_url,
        )
        return session.url
    except stripe.error.StripeError:
        logger.exception("Stripe error creating billing portal session for %s", customer_id)
        raise


def set_subscription_cancel_at_period_end(subscription_id: str, cancel: bool):
    """
    Schedule (or call off) a downgrade at the end of the paid-up period.

    Deliberately not an immediate cancellation: the member has paid through the
    end of the cycle, so they keep Premium until then. Stripe reports the
    subscription as 'active' the whole time and emits
    customer.subscription.deleted when it actually ends, which is what demotes
    the account to free.
    """
    try:
        return stripe.Subscription.modify(subscription_id, cancel_at_period_end=cancel)
    except stripe.error.StripeError:
        logger.exception(
            "Stripe error setting cancel_at_period_end=%s on %s", cancel, subscription_id
        )
        raise
