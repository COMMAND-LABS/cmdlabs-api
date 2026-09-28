"""
Delete account endpoint.
"""
import logging
import os

import stripe
from fastapi import APIRouter, HTTPException, status, Request, Response

from src.clients.stripe_client import cancel_subscription_now
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.rate_limit import limiter
from src.services import account_deletion

logger = logging.getLogger(__name__)

router = APIRouter()

@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def delete_account(
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request,
    response: Response
):
    """
    Delete the authenticated user's account.

    Cancels any Stripe subscription, hands rows they wrote in other people's
    orgs to those orgs' owners, and deletes the workspaces they own alone.
    Refused with 409 while they own an org that has other members in it.
    See services/account_deletion for the full sequence and why.

    After deletion, the JWT cookie is cleared.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    try:
        account_deletion.delete_account(db, account, cancel_subscription_now)
    except account_deletion.OwnsTeamError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("You own an organization with other members: "
                    f"{', '.join(exc.org_names)}. Transfer ownership or "
                    "remove its members before deleting your account."),
        )
    except stripe.error.StripeError:
        # Nothing has been written yet: Stripe is called before any change.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=("Your subscription could not be cancelled, so the account "
                    "was not deleted. Please try again or contact support."),
        )

    # Clear the JWT cookie
    response.delete_cookie(
        key="jwt",
        domain=os.getenv("COOKIE_DOMAIN"),
        path="/"
    )
        
    return Response(status_code=status.HTTP_204_NO_CONTENT)
