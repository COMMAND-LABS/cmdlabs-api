"""
Spaces: shared content, across organizations.

THE SECOND CONTAINER
--------------------
An ORG is a tenant. It holds private data — contacts, deals, contact events,
credentials — and `org_id == ctx.org_id` is the only thing that decides who
sees a row. That rule has no exceptions, and it is load-bearing precisely
because it has none.

A SPACE is the other kind of container. It holds SHARED content — courses,
documents, knowledge bases — and its members come from many different orgs. A
space is how one account publishes to people who do not work for them.

    Every row in the platform lives in exactly ONE of these.

A contact is in an org and can never be in a space. A course is in a space (or
in an org) and never both. The day a row can live in both, "who can see this?"
becomes a join across two membership tables, and nobody — including an auditor
— can answer it reliably. Dual-homed content tables therefore carry:

    CHECK ((org_id IS NULL) <> (space_id IS NULL))

OWNERSHIP IS ATTRIBUTION, NOT TENANCY
-------------------------------------
`Space.owner_org_id` records who is ACCOUNTABLE for a space: who pays for it,
who moderates it, whose org it is billed to. It must NEVER be read to decide
whether somebody may open the space's content.

This is the single most important line in this file. If owner_org_id meant
tenancy — the way org_id does everywhere else — then a space's content would
sit inside the owner's org, and a member from a different org reading it would
be a violation of the one rule the platform's isolation rests on. You would
then have to either break that rule or special-case it, and a tenancy rule with
special cases is one nobody can verify.

So: access to a space is decided by SpaceMember and nothing else. The same
distinction Course.account_id already makes ("attribution, never tenancy"), one
level up.

WHY A SEPARATE MODULE
---------------------
These tables belong to no tenant, and keeping that boundary visible in the
file layout is worth a little awkwardness. Registered on Base.metadata from db/models.py, so Alembic and
tests/conftest.py both see them.
"""
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)

from .database import Base

# How an outsider gets in. Ordered least → most open.
JOIN_INVITE = 'invite'        # the owner adds you; there is no way to ask
JOIN_REQUEST = 'request'      # you ask, the owner decides
JOIN_OPEN = 'open'            # you join yourself, no approval
JOIN_POLICIES = (JOIN_INVITE, JOIN_REQUEST, JOIN_OPEN)

# How a member came to be in the space. The audit answer to "why do they have
# access?", and deliberately the same vocabulary OrganizationMember uses, plus
# the door orgs do not have.
GRANTED_BY_GRANT = 'grant'                # the owner let them in, free
GRANTED_BY_SUBSCRIPTION = 'subscription'  # they paid for a tier
GRANTED_BY_REQUEST = 'request'            # they asked and were approved
SPACE_GRANTED_BY = (GRANTED_BY_GRANT, GRANTED_BY_SUBSCRIPTION,
                    GRANTED_BY_REQUEST)

REQUEST_PENDING = 'pending'
REQUEST_APPROVED = 'approved'
REQUEST_DENIED = 'denied'
REQUEST_STATUSES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DENIED)

SPACE_ACTIVE = 'active'
SPACE_ARCHIVED = 'archived'
SPACE_STATUSES = (SPACE_ACTIVE, SPACE_ARCHIVED)

# The tier every space starts with: what a member gets when nobody has set up
# anything more elaborate. Mirrors the seeded 'owner'/'member' pair on an org.
TIER_OWNER = 'owner'
TIER_MEMBER = 'member'


class Space(Base):
    """A place where content is shared across organizations."""
    __tablename__ = 'spaces'

    id = Column(Integer, primary_key=True, index=True)
    # No slug. It was added by symmetry with organizations, and organizations
    # then dropped theirs: the id already identifies a space in every route,
    # and a permanent public name is a decision with squatting and
    # link-stability consequences that nothing here needs yet. Easy to add
    # later; impossible to take back once links point at it.
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # ATTRIBUTION, NEVER TENANCY. See the module docstring — reading either of
    # these to authorize access to space content is the one mistake that turns
    # this design into a data leak.
    owner_account_id = Column(Integer, ForeignKey('accounts.id',
                                                  ondelete='SET NULL'),
                              nullable=True, index=True)
    owner_org_id = Column(Integer, ForeignKey('organizations.id',
                                              ondelete='SET NULL'),
                          nullable=True, index=True)

    # Whether it appears on the public browse page. A private space is reachable
    # only by people already in it, or invited by name.
    discoverable = Column(Boolean, nullable=False, server_default=text('false'))
    join_policy = Column(String(20), nullable=False, server_default=JOIN_INVITE)
    status = Column(String(20), nullable=False, server_default=SPACE_ACTIVE)

    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)

    __table_args__ = (
        CheckConstraint(
            "join_policy IN ('invite','request','open')",
            name='ck_spaces_join_policy'),
        CheckConstraint("status IN ('active','archived')",
                        name='ck_spaces_status'),
    )

    def __repr__(self):
        return f'<Space {self.id}: {self.name}>'


class SpaceTier(Base):
    """A named way to be in a space — free, or sold.

    The paywall, and the free invite, are ONE mechanism rather than two
    features. A tier with a `stripe_price_id` is purchasable; a tier without
    one can only be granted. That is the same shape OrganizationTier already
    has, and it is why "let this person in for free" needs no separate concept:
    it is a membership on a tier, recorded as granted rather than subscribed.
    """
    __tablename__ = 'space_tiers'

    id = Column(Integer, primary_key=True, index=True)
    space_id = Column(Integer, ForeignKey('spaces.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    tier_key = Column(String(64), nullable=False)
    label = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # Set when the space owner sells this tier through their own connected
    # Stripe account. NULL means invite-or-request only.
    stripe_price_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint('space_id', 'tier_key', name='uq_space_tier_key'),
    )

    def __repr__(self):
        return f'<SpaceTier space={self.space_id} {self.tier_key}>'


class SpaceMember(Base):
    """An account's membership in a space, and how they got it.

    THE ONLY THING that decides whether somebody may open a space's content.
    Not the owner's org, not their plan, not a group — this row.

    `granted_by` is what makes access auditable in one query: every door into a
    space leaves the same shape of row, differing only in the word recording
    which door it was.
    """
    __tablename__ = 'space_members'

    id = Column(Integer, primary_key=True, index=True)
    space_id = Column(Integer, ForeignKey('spaces.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    tier_key = Column(String(64), nullable=False, server_default=TIER_MEMBER)
    is_owner = Column(Boolean, nullable=False, server_default=text('false'))
    granted_by = Column(String(20), nullable=False,
                        server_default=GRANTED_BY_GRANT)
    # Who let them in. Attribution for the audit trail; never consulted for
    # access. NULL when they joined an open space themselves.
    invited_by_account_id = Column(Integer, ForeignKey('accounts.id',
                                                       ondelete='SET NULL'),
                                   nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint('space_id', 'account_id', name='uq_space_member'),
        CheckConstraint(
            "granted_by IN ('grant','subscription','request')",
            name='ck_space_member_granted_by'),
    )

    def __repr__(self):
        return (f'<SpaceMember space={self.space_id} '
                f'account={self.account_id} tier={self.tier_key}>')


class SpaceJoinRequest(Base):
    """Somebody asking to be let into a space.

    ONE row per (space, account), reused rather than re-inserted when somebody
    asks again after being turned down. That keeps the history answerable —
    "has this person been refused before?" is a single lookup — and stops a
    denied applicant from filling the owner's queue by re-submitting.

    No token, no expiry, for the same reason org invites have neither: the
    platform authenticates by OTP, so a second secret mailed to the same inbox
    would add a state machine and no security.
    """
    __tablename__ = 'space_join_requests'

    id = Column(Integer, primary_key=True, index=True)
    space_id = Column(Integer, ForeignKey('spaces.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    status = Column(String(20), nullable=False, server_default=REQUEST_PENDING)
    # What the applicant said for themselves. Shown to the owner deciding.
    message = Column(Text, nullable=True)
    decided_by_account_id = Column(Integer, ForeignKey('accounts.id',
                                                       ondelete='SET NULL'),
                                   nullable=True)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint('space_id', 'account_id', name='uq_space_join_request'),
        CheckConstraint("status IN ('pending','approved','denied')",
                        name='ck_space_join_request_status'),
    )

    def __repr__(self):
        return (f'<SpaceJoinRequest space={self.space_id} '
                f'account={self.account_id} {self.status}>')


class SpaceResource(Base):
    """A resource shared INTO a space by whoever owns it.

    WHY SHARING RATHER THAN DUAL-HOMING. A course is a pure enablement, so it
    moves into a space wholesale (courses.space_id, one home per row). An agent
    or a knowledge base cannot: it carries credentials, quotas and a billing
    relationship, all of which belong to the org that runs it. So it stays in
    its org and a space is granted READ access to it.

    That is a cross-org read arm, and it is the same one-directional shape the
    catalog had — with the platform-only restriction lifted. It is safe for the
    same reason: the row can only be added by somebody who OWNS the resource,
    so "Acme shares Acme's agent" is expressible and "Acme shares Beta's agent"
    is not. Enforced in the router, asserted in tests.

    This replaced catalog_items + catalog_grants. Two tables became one, and
    the audience went from "an org, optionally narrowed to a group" to "the
    members of a space" — which is the one membership question the platform now
    asks everywhere.
    """
    __tablename__ = 'space_resources'

    id = Column(Integer, primary_key=True, index=True)
    space_id = Column(Integer, ForeignKey('spaces.id', ondelete='CASCADE'),
                      nullable=False, index=True)
    # Deliberately narrow. CRM rows are tenant data and may never be shared —
    # the same whitelist the catalog carried, for the same reason.
    resource_type = Column(String(20), nullable=False)
    # No foreign key: the referenced table varies by resource_type, and a
    # dangling row is handled by the readers (it simply matches nothing)
    # rather than by a constraint that cannot span two tables.
    resource_id = Column(Integer, nullable=False)
    # Attribution, never authority. Who may share is re-checked at write time.
    added_by_account_id = Column(Integer, ForeignKey('accounts.id',
                                                     ondelete='SET NULL'),
                                 nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint('space_id', 'resource_type', 'resource_id',
                         name='uq_space_resource'),
        CheckConstraint("resource_type IN ('agent','vector_store')",
                        name='ck_space_resource_type'),
    )

    def __repr__(self):
        return (f'<SpaceResource space={self.space_id} '
                f'{self.resource_type}={self.resource_id}>')


# Resource types a space may share. Mirrors the CHECK above.
SHAREABLE_RESOURCE_TYPES = ('agent', 'vector_store')
