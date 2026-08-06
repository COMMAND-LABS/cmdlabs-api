"""Request and response shapes for the spaces API."""
from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from src.db.space_models import JOIN_POLICIES


class SpaceSummary(BaseModel):
    """A space as it appears in a list. Never includes its content."""
    id: int
    slug: str
    name: str
    description: Optional[str] = None
    discoverable: bool
    join_policy: str
    status: str
    member_count: int
    # About the CALLER, not the space: what this account may do with it.
    is_member: bool
    is_owner: bool
    # 'none' | 'pending' | 'approved' | 'denied' — so a browse card can say
    # "requested" instead of offering a button that would be refused.
    request_status: str
    created_at: Optional[datetime] = None


class SpaceMemberResponse(BaseModel):
    account_id: int
    email: str
    tier_key: str
    is_owner: bool
    # 'grant' | 'subscription' | 'request': which door they came through.
    granted_by: str
    created_at: Optional[datetime] = None


class SpaceTierResponse(BaseModel):
    tier_key: str
    label: str
    description: Optional[str] = None
    # True when the owner has attached a price. The paywall, as one boolean —
    # the price id itself is not the caller's business.
    purchasable: bool


class JoinRequestResponse(BaseModel):
    id: int
    account_id: int
    email: str
    status: str
    message: Optional[str] = None
    created_at: Optional[datetime] = None


class SpaceDetail(SpaceSummary):
    """One space in full.

    `members` and `join_requests` are populated for OWNERS only and empty for
    everybody else — who else is in a space is the owner's business, not a
    fact every member may enumerate.
    """
    tiers: List[SpaceTierResponse] = []
    members: List[SpaceMemberResponse] = []
    join_requests: List[JoinRequestResponse] = []


class CreateSpaceRequest(BaseModel):
    slug: str = Field(description="Public identifier, lowercase. Permanent.")
    name: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    discoverable: bool = False
    join_policy: str = "invite"

    @field_validator("slug")
    @classmethod
    def _normalize(cls, v: str) -> str:
        return (v or "").strip().lower()

    @field_validator("join_policy")
    @classmethod
    def _known_policy(cls, v: str) -> str:
        if v not in JOIN_POLICIES:
            raise ValueError(f"join_policy must be one of {JOIN_POLICIES}")
        return v


class UpdateSpaceRequest(BaseModel):
    """The slug is absent on purpose: it is the space's public identity."""
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = None
    discoverable: Optional[bool] = None
    join_policy: Optional[str] = None

    @field_validator("join_policy")
    @classmethod
    def _known_policy(cls, v):
        if v is not None and v not in JOIN_POLICIES:
            raise ValueError(f"join_policy must be one of {JOIN_POLICIES}")
        return v


class InviteToSpaceRequest(BaseModel):
    email: str
    tier_key: str = "member"

    @field_validator("email")
    @classmethod
    def _looks_like_an_address(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v or " " in v:
            raise ValueError("Enter a valid email address.")
        return v


class JoinRequestBody(BaseModel):
    message: Optional[str] = Field(default=None, max_length=1000)
