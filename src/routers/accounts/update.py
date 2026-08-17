"""
Update account endpoint.
"""
from fastapi import APIRouter, HTTPException, status, Request
from src.deps import db_dependency, jwt_dependency, account_id_from_claims, ensure_account
from src.db.models import Account
from .models import UpdateAccountRequest, AccountResponse
from src.rate_limit import limiter

router = APIRouter()

@router.put("/me", response_model=AccountResponse)
@limiter.limit("10/minute")
async def update_account(
    request_body: UpdateAccountRequest,
    db: db_dependency,
    jwt: jwt_dependency,
    request: Request
):
    """
    Update the authenticated user's account.
    
    Updatable fields:
    - email: The account email address
    - name: Display name (whitespace-only clears it)
    - newsletter_subscribed: Newsletter subscription preference

    Note: Password changes should use the /auth/reset-password flow.
    """
    account_id = account_id_from_claims(jwt)
    account = ensure_account(db, account_id)

    # Check if at least one field is being updated
    if (
        request_body.email is None
        and request_body.name is None
        and request_body.newsletter_subscribed is None
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one field (email, name, or newsletter_subscribed) must be provided for update"
        )
        
    # Update email if provided
    if request_body.email is not None:
        email = request_body.email.strip().lower()
        if not email:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Email cannot be empty"
            )
            
        # Check if email is already taken by another account
        existing_account = db.query(Account).filter(
            Account.email == email,
            Account.id != account_id
        ).first()
            
        if existing_account:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Email address is already in use"
            )
            
        account.email = email
        
    # Update name if provided; a whitespace-only value clears it back to NULL
    # (the column is optional, so there has to be a way back out).
    if request_body.name is not None:
        account.name = request_body.name.strip() or None

    # Update newsletter_subscribed if provided
    if request_body.newsletter_subscribed is not None:
        account.newsletter_subscribed = request_body.newsletter_subscribed
        
    # Commit the changes
    db.commit()
    db.refresh(account)
        
    return AccountResponse(
        id=account.id,
        email=account.email,
        name=account.name,
        newsletter_subscribed=account.newsletter_subscribed,
        stripe_customer_id=account.stripe_customer_id,
        is_super_admin=account.is_super_admin,
        subscription_status=account.subscription_status,
        subscription_active=account.has_active_subscription
    )
