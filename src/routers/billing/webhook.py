"""
Stripe webhook — the only writer of subscription state.

Access is granted from the subscription status reported here, never from "the
customer has a card attached": an attached card that was never successfully
charged is not a paying member.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Request, status
from sqlalchemy.orm import Session
import stripe

from src.deps import db_dependency
from src.db.models import Account, role_for_subscription
from src.clients.stripe_client import construct_webhook_event, get_subscription

logger = logging.getLogger(__name__)

router = APIRouter()

# Events we act on. Anything else is acknowledged and ignored, so enabling extra
# events in the Stripe dashboard can never 500 the endpoint.
CHECKOUT_COMPLETED = "checkout.session.completed"
SUBSCRIPTION_EVENTS = (
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
)


def _to_datetime(unix_ts) -> datetime | None:
    """Stripe sends period ends as unix seconds; store them as aware UTC."""
    if not unix_ts:
        return None
    try:
        return datetime.fromtimestamp(int(unix_ts), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return None


def _find_account(
    db: Session,
    account_id: str | None = None,
    subscription_id: str | None = None,
    customer_id: str | None = None,
) -> Account | None:
    """
    Resolve the account an event belongs to, most reliable signal first.

    account_id is the id we put on the session/subscription ourselves, so it is
    trusted above the Stripe-side ids; the other two cover events that reach us
    without our metadata (a subscription created in the Stripe dashboard, say).
    """
    if account_id:
        try:
            account = db.query(Account).filter(Account.id == int(account_id)).first()
            if account:
                return account
        except (TypeError, ValueError):
            logger.warning("[STRIPE WEBHOOK] Unusable account_id %r", account_id)

    if subscription_id:
        account = db.query(Account).filter(
            Account.stripe_subscription_id == subscription_id
        ).first()
        if account:
            return account

    if customer_id:
        return db.query(Account).filter(
            Account.stripe_customer_id == customer_id
        ).first()

    return None


def _apply_subscription(account: Account, subscription) -> None:
    """
    Copy the subscription's current state onto the account, and move the role
    to match in the same transaction.

    Role and subscription status are written together and only here, so the two
    can never disagree — a lapsed subscription demotes to 'free' on the same
    commit that records the lapse. Admins pass through untouched.
    """
    account.stripe_subscription_id = subscription.get("id")
    account.subscription_status = subscription.get("status")
    account.subscription_current_period_end = _to_datetime(
        subscription.get("current_period_end")
    )
    account.role = role_for_subscription(account.subscription_status, account.role)


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def stripe_webhook(
    request: Request,
    db: db_dependency,
    stripe_signature: str = Header(None, alias="stripe-signature"),
):
    """
    Receive Stripe billing events.

    Deliberately unauthenticated (Stripe holds no session) but never unverified:
    the signature check is what stands between this endpoint and anyone handing
    themselves a free membership. Not rate-limited — throttling Stripe's retries
    would drop real payment events.
    """
    payload = await request.body()

    try:
        event = construct_webhook_event(payload, stripe_signature)
    except ValueError as e:
        # Missing secret or malformed body.
        logger.error("[STRIPE WEBHOOK] Rejected payload: %s", e)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook payload",
        )
    except stripe.error.SignatureVerificationError:
        logger.error("[STRIPE WEBHOOK] Signature verification failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid webhook signature",
        )

    event_type = event["type"]
    data = event["data"]["object"]

    try:
        if event_type == CHECKOUT_COMPLETED:
            # Only subscription checkouts carry membership.
            if data.get("mode") != "subscription":
                return {"received": True, "handled": False}

            subscription_id = data.get("subscription")
            account = _find_account(
                db,
                account_id=data.get("client_reference_id"),
                subscription_id=subscription_id,
                customer_id=data.get("customer"),
            )
            if not account:
                # Retrying will not conjure the account, so acknowledge and log
                # loudly rather than making Stripe redeliver for days.
                logger.error(
                    "[STRIPE WEBHOOK] %s for unknown account (ref=%s customer=%s)",
                    event_type, data.get("client_reference_id"), data.get("customer"),
                )
                return {"received": True, "handled": False}

            # The session says a payment succeeded but carries no status, so read
            # the subscription itself — that is what entitlement is based on.
            subscription = get_subscription(subscription_id) if subscription_id else None
            if subscription:
                _apply_subscription(account, subscription)
            if data.get("customer") and not account.stripe_customer_id:
                account.stripe_customer_id = data["customer"]
            db.commit()
            logger.info(
                "[STRIPE WEBHOOK] Account %s subscription %s -> %s",
                account.id, account.stripe_subscription_id, account.subscription_status,
            )
            return {"received": True, "handled": True}

        if event_type in SUBSCRIPTION_EVENTS:
            metadata = data.get("metadata") or {}
            account = _find_account(
                db,
                account_id=metadata.get("account_id"),
                subscription_id=data.get("id"),
                customer_id=data.get("customer"),
            )
            if not account:
                logger.error(
                    "[STRIPE WEBHOOK] %s for unknown account (sub=%s customer=%s)",
                    event_type, data.get("id"), data.get("customer"),
                )
                return {"received": True, "handled": False}

            _apply_subscription(account, data)
            if event_type == "customer.subscription.deleted":
                # Stripe sends this as status 'canceled', but pin it so a future
                # payload shape cannot leave a deleted subscription looking live
                # — and re-derive the role from the pinned status.
                account.subscription_status = "canceled"
                account.role = role_for_subscription("canceled", account.role)
            db.commit()
            logger.info(
                "[STRIPE WEBHOOK] Account %s subscription %s -> %s",
                account.id, account.stripe_subscription_id, account.subscription_status,
            )
            return {"received": True, "handled": True}

        # Anything else: acknowledged, untouched.
        return {"received": True, "handled": False}

    except stripe.error.StripeError:
        db.rollback()
        logger.exception("[STRIPE WEBHOOK] Stripe call failed handling %s", event_type)
        # 500 so Stripe retries — this one is worth retrying.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not process event",
        )
    except Exception:
        db.rollback()
        logger.exception("[STRIPE WEBHOOK] Failed handling %s", event_type)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Could not process event",
        )
