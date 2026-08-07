"""
Billing router — Stripe Checkout, the billing portal, and the Stripe webhook.

Subscription state lives on Account (stripe_subscription_id,
subscription_status, subscription_current_period_end) and is written only by
the webhook. accounts.is_staff is untouched by billing: staff decides which dashboard
you get, the subscription decides whether the paid modules are unlocked.
"""
from fastapi import APIRouter

from .checkout import router as checkout_router
from .webhook import router as webhook_router

router = APIRouter()

router.include_router(checkout_router)
router.include_router(webhook_router)
