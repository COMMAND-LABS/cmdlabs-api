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
from src.db.models import ACTIVE_SUBSCRIPTION_STATUSES, Account, Organization
from src.services import audit
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


def _record_billing_transition(db, account: Account, event_type: str) -> None:
    """Log the lapse (or the recovery) against the orgs it actually affects.

    Against the ORGS rather than the account, because the org is where the
    consequence lands and where somebody investigating starts. Comped orgs are
    skipped for the same reason billing cannot make them read-only: staff set
    their ceiling by hand, so a payment says nothing about them.
    """
    orgs = (db.query(Organization.id)
              .filter(Organization.owner_account_id == account.id,
                      Organization.ceiling_managed_by == "subscription")
              .all())
    for (org_id,) in orgs:
        audit.record_org_change(
            db, event_type=event_type, org_id=org_id,
            detail=f"subscription {account.subscription_status}",
            actor_account_id=account.id)


def _apply_subscription(db, account: Account, subscription) -> None:
    """
    Copy the subscription's current state onto the account.

    THE ONLY PLACE A LAPSE IS RECORDED. Paid-ness and the module ceiling are
    both READ from the status set here (config/plans_registry,
    services/modules), so there is no cached plan and no ceiling to backfill.
    The one thing that cannot be read from the status is WHEN it stopped being
    an entitling one, and that is the timestamp below.

    Written on the TRANSITION only. Re-stamping subscription_lapsed_at on every
    subsequent webhook for an already-lapsed subscription would restart the
    grace window each time Stripe retried a failed charge — which is both a way
    to keep premium indefinitely and a clock the customer cannot predict.
    """
    was_entitled = account.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES

    account.stripe_subscription_id = subscription.get("id")
    account.subscription_status = subscription.get("status")
    account.subscription_current_period_end = _to_datetime(
        subscription.get("current_period_end")
    )

    now_entitled = account.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES

    if now_entitled:
        # Paid again: the window closes and the org is writable on the next
        # request. Cleared unconditionally, so a customer who lapsed and came
        # back does not carry a stale timestamp into their next lapse.
        account.subscription_lapsed_at = None
    elif was_entitled or account.subscription_lapsed_at is None:
        # Just lapsed, or lapsed at some point before this column existed.
        # Either way this is the first instant we can honestly point at.
        account.subscription_lapsed_at = datetime.now(timezone.utc)


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
                _apply_subscription(db, account, subscription)
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

            if event_type == "customer.subscription.deleted":
                # Stripe sends this as status 'canceled', but pin it so a future
                # payload shape cannot leave a deleted subscription looking
                # live. Pinned BEFORE _apply_subscription so the lapse timestamp
                # is decided from the status we actually mean.
                data = {**data, "status": "canceled"}

            was_entitled = (account.subscription_status
                            in ACTIVE_SUBSCRIPTION_STATUSES)
            _apply_subscription(db, account, data)
            now_entitled = (account.subscription_status
                            in ACTIVE_SUBSCRIPTION_STATUSES)

            # The audit trail for the transition. Only the edges are logged:
            # the grace window ENDING is a comparison against the timestamp
            # above, not an event anything performs, so there is nothing to
            # record when it does.
            if was_entitled and not now_entitled:
                _record_billing_transition(db, account, audit.ORG_SUSPEND)
            elif now_entitled and not was_entitled:
                _record_billing_transition(db, account, audit.ORG_RESTORE)

            db.commit()
            logger.info(
                "[STRIPE WEBHOOK] Account %s subscription %s -> %s (lapsed_at=%s)",
                account.id, account.stripe_subscription_id,
                account.subscription_status, account.subscription_lapsed_at,
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
