from sqlalchemy import Column, Integer, String, ForeignKey, UUID, JSON, DateTime, Date, func, Double, Float, Numeric, Enum, Text, Boolean, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM, JSONB
from .database import Base
from .service_name import ServiceName
import datetime
import uuid

# An account is one of two things, and they are stored differently on purpose:
#
#   SUPER ADMIN  accounts.is_super_admin — granted and revoked out of band by
#                scripts/super_admin.py, never by an API path
#   PAYING       derived per request from subscription_status, stored nowhere
#                (config/plans_registry.plan_for_account → 'free' | 'premium')
#
# There used to be a single `role` column holding admin/premium/free. Two of
# those three values were a cache of Stripe, and a cache with no invalidation
# is a value that drifts — hence the reconciliation script that existed only to
# drag it back. Deriving paid-ness removes the drift by removing the copy.
#
# (SpaceMember.tier_key was the other per-container tier and had nothing to do
# with billing either. It went with spaces.)

# Stripe subscription statuses that mean "this account has paid and is entitled
# to the Premium features". Deliberately excludes past_due/unpaid/incomplete:
# an attached card that was never successfully charged is not a paying member.
ACTIVE_SUBSCRIPTION_STATUSES = ('active', 'trialing')


# role_for_subscription() lived here and is gone with the `role` column it
# maintained. Its whole job was keeping a cached free/premium value in step
# with subscription_status; paid-ness is now read from that status directly,
# via config/plans_registry.plan_for_account(), so there is nothing left to
# keep in step. Cancellation still takes effect on the same request, because a
# derived value cannot lag.


class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    reset_token = Column(String)
    stripe_customer_id = Column(String, nullable=True)
    newsletter_subscribed = Column(Boolean, default=False, nullable=False)
    # Platform super admin. Granted and revoked out of band by
    # scripts/super_admin.py — no API path sets it in either direction, so a
    # compromised account cannot escalate itself and there is no "make super
    # admin" button to click by accident.
    #
    # This replaced a `role` column that also carried 'premium'/'free'. Those
    # were a CACHE of subscription_status, which is the fact; keeping both meant
    # keeping them in agreement, which is what role_for_subscription() and a
    # reconciliation script existed to do. Paid-ness is now derived per request
    # (config/plans_registry.plan_for_account) and stored nowhere.
    is_super_admin = Column(Boolean, nullable=False, default=False,
                            server_default=text('false'), index=True)
    # Subscription state, owned entirely by the Stripe webhook — nothing else
    # writes these. Entitlement is read from subscription_status, never from
    # "the customer has a payment method attached".
    #
    # No CHECK constraint on the status: the vocabulary belongs to Stripe and a
    # status they add later must not start rejecting webhook writes.
    stripe_subscription_id = Column(String, nullable=True, index=True)
    subscription_status = Column(String(30), nullable=True, index=True)
    subscription_current_period_end = Column(DateTime(timezone=True), nullable=True)
    # WHEN the subscription stopped being an entitling one. Set by the webhook
    # on the transition out of active/trialing, cleared on the way back in.
    #
    # THE ONLY THING STORED ABOUT A LAPSE. Everything a lapse causes is a
    # comparison against this instant: within GRACE_DAYS the org keeps its
    # modules and is refused writes; past it, the plan drops to free. There is
    # no suspended flag, no scheduled job, and therefore nothing that can
    # disagree with Stripe — see config/plans_registry.billing_state.
    subscription_lapsed_at = Column(DateTime(timezone=True), nullable=True)
    login_otp = Column(String, nullable=True)
    login_otp_expires_at = Column(DateTime(timezone=True), nullable=True)
    # Which org this account lands in when no active-org cookie is present.
    # Nullable only so the schema tolerates an account created before its
    # membership exists (see services/organizations.ensure_membership).
    default_org_id = Column(Integer, ForeignKey('organizations.id', ondelete='SET NULL'),
                            nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    logins = relationship('Logins', back_populates='account')
    chat_sessions = relationship('ChatSession', back_populates='account')
    usage_credits = relationship('UsageCredits', back_populates='account')
    credentials = relationship('Credential', back_populates='account', cascade='all, delete-orphan')
    vector_db_logs = relationship('VectorDbIngestionLog', back_populates='account')
    api_keys = relationship('ApiKey', back_populates='account', cascade='all, delete-orphan')
    leads = relationship('Lead', back_populates='account', cascade='all, delete-orphan')
    contacts = relationship('Contact', back_populates='account', cascade='all, delete-orphan')
    companies = relationship('Company', back_populates='account', cascade='all, delete-orphan')
    contact_lists = relationship('ContactList', back_populates='account', cascade='all, delete-orphan')
    deals = relationship('Deal', back_populates='account', cascade='all, delete-orphan')
    prompts = relationship('Prompt', back_populates='account', cascade='all, delete-orphan')
    org_memberships = relationship('OrganizationMember', back_populates='account',
                                   foreign_keys='OrganizationMember.account_id',
                                   cascade='all, delete-orphan')
    tool_approvals = relationship('PendingToolApproval', back_populates='account', cascade='all, delete-orphan')
    email_events = relationship('EmailEvent', back_populates='account', cascade='all, delete-orphan')
    email_templates = relationship('EmailTemplate', back_populates='account', cascade='all, delete-orphan')
    email_campaigns = relationship('EmailCampaign', back_populates='account', cascade='all, delete-orphan')
    email_campaign_ratings = relationship('EmailCampaignRating', back_populates='account', cascade='all, delete-orphan')

    __table_args__ = (
    )

    @property
    def has_active_subscription(self) -> bool:
        """Whether this account is entitled to the paid Member features."""
        return self.subscription_status in ACTIVE_SUBSCRIPTION_STATUSES

    def __repr__(self):
        return f'<Account {self.email}>'


class Organization(Base):
    """
    A tenant. The unit of data isolation on the platform, with no exceptions.

    EVERY account owns one. A signup gets a personal workspace — an org with a
    single member who is its owner — and a team is the same object with more
    members in it. There is no "not really an org" case, which is what lets the
    tenancy rule be `org_id == ctx.org_id` and nothing else.

    That uniformity replaced a `data_scope` column. Root used to hold every
    signup at once, and data_scope='personal' was the flag that stopped them
    seeing each other; it existed for exactly one row, and it was the only
    reason visibility depended on anything besides org_id. Migration
    e3f4a5b6c7d8 split the orgs apart and f4a5b6c7d8e9 dropped the column.

    NO SLUG, AND NO SPECIAL ORG. Organizations used to carry an immutable
    public `slug`, and the one whose slug was 'root' was the platform's own —
    the home of catalog content and the org super admins had to be placed in to
    work. Both jobs are gone: super admins bypass the module ceiling wherever
    they are, and publishing became a Space (itself since removed). An id
    identifies an org in every
    route, so the slug was a permanent public name carrying squatting and
    link-stability consequences that nothing needed. Cheap to reintroduce;
    impossible to withdraw once links point at it.

    THE CEILING IS ALWAYS A PLAN, and `pinned_plan` is the only thing stored
    about it. Which modules this org may use at all is
    plans_registry.PLAN_MODULES[plan], where plan is either pinned here or read
    from the owner's subscription. For a PERSONAL org that is the whole
    entitlement, because its single member is its owner and an owner bypasses
    the tier layer; tiers only start mattering once an org has somebody in it
    who is not the owner. Resolved at read time (services/modules.py), so a
    change to a plan reaches every org on their next request.
    """
    __tablename__ = 'organizations'

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    owner_account_id = Column(Integer, ForeignKey('accounts.id', ondelete='SET NULL'),
                              nullable=True, index=True)
    # The plan this org gets NO MATTER WHAT ITS OWNER PAYS. NULL — the normal
    # case — means "follow the owner's subscription".
    #
    # This is the comp, and it is the whole of it. The same asymmetry
    # OrganizationMember.granted_by encodes one level down: super admins giving
    # somebody access is a promise, and no webhook may quietly withdraw it.
    #
    # A PLAN, NOT A LIST OF MODULES. It used to be a frozen `granted_modules`
    # snapshot guarded by a `ceiling_managed_by` flag, and a snapshot is a
    # cache: every module added to a plan afterwards never reached a comped
    # org. All three comped orgs on the platform silently ended up without
    # `courses` and `spaces` that way. Pinning the plan tracks PLAN_MODULES as
    # it grows, so there is nothing left to backfill and nothing to go stale.
    pinned_plan = Column(String(20), nullable=True)
    # NO status COLUMN. There used to be one holding 'active' | 'read_only',
    # enforced on every write and written by absolutely nothing — every org on
    # the platform sat at 'active' including the one whose subscription had
    # been cancelled, so the protection it appeared to provide had never once
    # fired.
    #
    # Read-only is now DERIVED from the owner's accounts.subscription_lapsed_at
    # (config/plans_registry.billing_state, resolved per request in deps.py).
    # A stored copy could only ever be a cache of that, and a cache of a
    # billing fact with no invalidation path is the exact shape of bug that
    # made the ceiling wrong three times.
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        # NULL passes a CHECK, which is what makes "follow billing" the default
        # without a sentinel value standing in for it.
        CheckConstraint("pinned_plan IN ('free','premium')",
                        name='ck_org_pinned_plan'),
    )

    owner = relationship('Account', foreign_keys=[owner_account_id])
    members = relationship('OrganizationMember', back_populates='org',
                           cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Organization {self.id}: {self.name}>'


class OrganizationMember(Base):
    """
    An account's membership in an org, carrying the ROLE it holds there.

    The same account may be a member of several orgs in different roles — you
    can manage the org that employs you and be a community member of another.

    `role` is one of config/roles_registry.ROLE_KEYS: 'manager' for the core
    team, 'community_member' for people the org serves. It replaced `tier_key`,
    which named a row in organization_tiers — a per-org, owner-editable matrix
    of arbitrary module sets. Three platform-wide roles cost an owner the
    ability to define their own bundles and buy back an answer to "what can
    this person do?" that means the same thing in every org.

    granted_by is the override that makes comping work:
      'subscription' - owned by the Stripe webhook; lapses when billing does.
      'grant'        - set by an owner; NEVER written by any webhook.

    OWNERSHIP IS NOT HERE, AND IS NOT A ROLE VALUE. It is
    organizations.owner_account_id, and nowhere else. There used to be an
    is_owner column on this table too, which made ownership a fact stored twice
    with nothing keeping the copies in step — and they drifted: orgs whose
    owner_account_id named an account holding no is_owner row, so the owner
    could not open the org they owned. deps.py derives it now
    (`org.owner_account_id == account_id`) off a row it has already joined, so
    the two cannot disagree because there is only one.

    That history is exactly why 'owner' is NOT admitted by ck_org_member_role.
    Adding it would recreate the drift in a new column: two places claiming to
    know who owns the org, and a CHECK constraint that cannot compare them.

    Ownership remains a module BYPASS rather than a stored set of grants: an
    owner always reaches every module the org's ceiling allows, so their role is
    inert — which is why no UI should show an owner's role as though it granted
    them anything.
    """
    __tablename__ = 'organization_members'

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    # Kept in step with config/roles_registry.ROLE_KEYS. Defaulted to the
    # SMALLER role so a row written without one grants the least, never the
    # most.
    role = Column(String(32), nullable=False, server_default='community_member')
    granted_by = Column(String(20), nullable=False, server_default='grant')
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'account_id', name='uq_org_member'),
        CheckConstraint("granted_by IN ('subscription','grant')",
                        name='ck_org_member_granted_by'),
        # No 'owner'. See the docstring — ownership is a column on
        # organizations, and admitting it here would store it twice.
        CheckConstraint("role IN ('manager','community_member')",
                        name='ck_org_member_role'),
    )

    org = relationship('Organization', back_populates='members')
    account = relationship('Account', back_populates='org_memberships',
                           foreign_keys=[account_id])

    def __repr__(self):
        return (f'<OrganizationMember org={self.org_id} account={self.account_id} '
                f'role={self.role}>')


class Logins(Base):
    __tablename__ = 'logins'
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'))
    created_at = Column(DateTime(timezone=True), default=func.now())
    ip_address = Column(String, nullable=False)

    account = relationship('Account', back_populates='logins')
    
    def __repr__(self):
        return f'<Login {self.created_at}>'
    
class ChatHistory(Base):
    __tablename__ = 'chat_history'
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(UUID, nullable=False)
    message = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())

class ChatSession(Base):
    __tablename__ = 'chat_sessions'
    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(UUID, unique=True, index=True)
    agent_id = Column(Integer, ForeignKey('agents.id', ondelete='CASCADE'), nullable=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    # Optional binding to a single CRM contact. When set, this session is the
    # server-trusted scope for the contact-scoped agent: scoped tools run
    # against this contact only. SET NULL on contact delete clears the scope
    # (the contact agent then fails closed rather than running unscoped).
    #
    # Assumption: a contact never changes account. The contact<->account match
    # is validated at session creation; the per-tool account_id filter is the
    # runtime backstop. Revisit this binding if a "transfer contact" feature
    # is ever added.
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
    title = Column(String)

    account = relationship('Account', back_populates='chat_sessions')
    agent = relationship('Agent', back_populates='chat_sessions')
    contact = relationship('Contact')
    messages = relationship('ChatMessage', back_populates='session', cascade='all, delete-orphan')
    
    def __repr__(self):
        return f'<ChatSession {self.session_id}>'

class ChatMessage(Base):
    __tablename__ = 'chat_messages'
    id = Column(Integer, primary_key=True, index=True)
    chat_session_id = Column(Integer, ForeignKey('chat_sessions.id', ondelete='CASCADE'), nullable=False, index=True)
    message = Column(JSON)
    created_at = Column(DateTime(timezone=True), default=func.now())
    
    session = relationship('ChatSession', back_populates='messages')
    
    def __repr__(self):
        return f'<ChatMessage {self.id}>'

class UsageCredits(Base):
    __tablename__ = 'usage_credits'
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    amount = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    account = relationship('Account', back_populates='usage_credits')
    
    def __repr__(self):
        return f'<UsageCredits {self.account_id}: ${self.amount}>'

class Credential(Base):
    """
    Stores encrypted credentials for third-party services.
    
    The table supports multiple credential types:
    - API keys: Simple key-value (e.g., OpenAI API key)
    - Database connections: Host, port, username, password, database name
    - OAuth: Client ID, client secret, tokens
    - SSH keys: Private keys with optional passphrases
    - Certificates: Certificate data with optional private keys
    
    All credentials are stored in encrypted_data as encrypted JSON structures.
    """
    __tablename__ = 'credentials'
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    credential_type = Column(Enum(ServiceName, name='credential_type_enum'), nullable=False, index=True)
    auth_type = Column(String(50), nullable=False, index=True, default='api_key')
    credential_name = Column(String(255), nullable=True, index=True)

    # Encrypted storage (JSON structure, encrypted with Fernet)
    encrypted_data = Column(Text, nullable=False)
    
    # Non-sensitive metadata (e.g., display name, description, last_validated)
    credential_metadata = Column(JSON, nullable=True)
    
    created_at = Column(DateTime(timezone=True), default=func.now())
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now())
    
    account = relationship('Account', back_populates='credentials')
    # Sharing is recorded in the unified access_grants table (resource_type
    # 'credential'); see services/access.py. No per-resource relationship.

    def __repr__(self):
        name = self.credential_name or self.credential_type
        return f'<Credential {name} ({self.auth_type}) for account {self.account_id}>'


class CredentialDefault(Base):
    """
    A per-account, per-credential-type default selection.

    "Default" is NOT a flag on the credential itself: a shared credential can be
    one account's default while its owner keeps a different default. Each account
    has at most one default per credential_type (ServiceName), chosen from any
    credential it can use (owned OR shared with it).

    The credential_id FK cascades on delete, so deleting a credential
    automatically clears anyone's default that pointed at it. Defaults that lose
    their backing access (credential unshared, member removed from a group) are
    pruned explicitly via credential_access.prune_unusable_defaults_for_account.
    """
    __tablename__ = 'credential_defaults'

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    # Reuse the existing PG enum created for credentials.credential_type.
    credential_type = Column(Enum(ServiceName, name='credential_type_enum', create_type=False), nullable=False, index=True)
    credential_id = Column(Integer, ForeignKey('credentials.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('account_id', 'credential_type', name='uq_credential_default_account_type'),
    )

    account = relationship('Account', foreign_keys=[account_id])
    credential = relationship('Credential', foreign_keys=[credential_id])

    def __repr__(self):
        return f'<CredentialDefault account={self.account_id} type={self.credential_type} -> credential={self.credential_id}>'


class ApiKeyStatus(str, Enum):
    """Enumeration of API key statuses."""
    ACTIVE = "active"
    REVOKED = "revoked"


class ApiKey(Base):
    __tablename__ = 'api_keys'
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    
    # Key storage: hash the full key, store prefix for display/lookup
    key_hash = Column(String, nullable=False, unique=True, index=True)
    key_prefix = Column(String, nullable=False, index=True)  # First 20 chars for display/lookup
    
    # Optional metadata
    name = Column(String, nullable=True)  # User-friendly name (e.g., "Website Chatbot")
    status = Column(PG_ENUM('active', 'revoked', name='api_key_status_enum', create_type=False), nullable=False, default=ApiKeyStatus.ACTIVE, index=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    
    account = relationship('Account', back_populates='api_keys')
    
    def __repr__(self):
        return f'<ApiKey {self.key_prefix}... for account {self.account_id}>'


class OperationType(str, Enum):
    """Enumeration of vector database operation types."""
    INGEST = "INGEST"
    DELETE = "DELETE"
    UPDATE = "UPDATE"


class OperationStatus(str, Enum):
    """Enumeration of vector database operation statuses."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"
    PENDING = "PENDING"


class VectorDbIngestionLog(Base):
    __tablename__ = 'vector_db_ingestion_log'
    
    # Primary Key (UUID)
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), default=func.now(), index=True)
    
    # Operation Details
    # Note: Enum types are created in migration, using create_type=False here
    operation_type = Column(
        PG_ENUM('INGEST', 'DELETE', 'UPDATE', name='operation_type_enum', create_type=False),
        nullable=False,
        index=True
    )
    status = Column(
        PG_ENUM('SUCCESS', 'FAILED', 'PARTIAL', 'PENDING', name='operation_status_enum', create_type=False),
        nullable=False,
        index=True
    )
    
    # User/Account
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    
    # Vector Database Info
    provider = Column(String, nullable=False)  # 'pinecone', 'chroma', etc.
    index_name = Column(String, nullable=False, index=True)
    namespace = Column(String, nullable=True, index=True)
    
    # File Information
    filenames = Column(JSON, nullable=True)  # Array of filenames
    comment = Column(Text, nullable=True)

    # Pointer back to the original source document in Google Cloud Storage.
    # Nullable for backward compatibility with rows ingested before per-account
    # GCS storage existed. The same pointer is mirrored into each vector's
    # metadata so embeddings can resolve back to the original file.
    gcs_bucket = Column(String, nullable=True)
    gcs_file_path = Column(String, nullable=True)

    # Vector Counts
    vectors_added = Column(Integer, default=0)
    vectors_deleted = Column(Integer, default=0)
    vectors_failed = Column(Integer, default=0)
    
    # Error Handling
    error_message = Column(Text, nullable=True)
    error_code = Column(String, nullable=True)
    
    # Batch Grouping
    batch_number = Column(String, nullable=True, index=True)  # UUID for grouping related operations
    
    # Relationships
    account = relationship('Account', back_populates='vector_db_logs')
    
    def __repr__(self):
        return f'<VectorDbIngestionLog {self.id} - {self.operation_type.value} - {self.status.value}>'


class Agent(Base):
    __tablename__ = 'agents'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    # Intra-org visibility. 'private' = creator plus explicit grantees;
    # 'org' = every member. Defaults to 'private' so widening is always a
    # deliberate act.
    visibility = Column(String(20), nullable=False, server_default='private')
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    name = Column(String, nullable=False, index=True)
    config = Column(JSON, nullable=True)  # JSONB in PostgreSQL, JSON in SQLAlchemy
    
    chat_sessions = relationship('ChatSession', back_populates='agent', cascade='all, delete-orphan')
    # Sharing is recorded in access_grants (resource_type 'agent'); see services/access.py.

    def __repr__(self):
        return f'<Agent {self.id}: {self.name}>'


class Lead(Base):
    """
    Stores lead/inquiry information.
    
    Leads are potential customers or inquiries captured through
    various channels (website forms, chat, etc.).
    """
    __tablename__ = 'leads'
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id'), nullable=False, index=True)
    chat_session_id = Column(UUID, nullable=True, index=True)  # UUID of the chat session where lead was captured
    name = Column(String(255), nullable=False)
    email = Column(String(255), nullable=True, index=True)
    phone = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)
    
    account = relationship('Account', back_populates='leads')
    
    def __repr__(self):
        return f'<Lead {self.id}: {self.name}>'


class Prompt(Base):
    """
    Stores reusable prompt templates.
    
    Prompts are text templates that can be saved and reused
    across different agents or contexts.
    """
    __tablename__ = 'prompts'
    
    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)
    
    account = relationship('Account', back_populates='prompts')
    
    def __repr__(self):
        return f'<Prompt {self.id}: {self.name}>'


class Course(Base):
    """
    A course an organization may open.

    The CONTENT is not here and never will be. A course is a dynamic experience
    living in the Next.js router — components, embedded agents, interactive
    steps — so what this table stores is a per-org ENABLEMENT: which stable
    course_key this tenant may reach, and how it is titled and ordered for them.

    That is also why courses need no publishing mechanism. The content is
    platform code, so two orgs holding the same key render the same route, the
    way both render /dashboard/contacts. One copy exists by construction and no
    tenant data moves, so there is nothing to leak between them.

    `course_key` is a STABLE IDENTIFIER matching a UI route, never a display
    name — renaming the folder must not revoke access. Same rule as
    config/modules_registry.py, for the same reason.

    ONE HOME PER ROW
    ----------------
    A course lives in an ORG. `org_id` is NOT NULL, and every member of that org
    may open it.

    It used to be dual-homed — org_id OR space_id, never both and never neither,
    enforced by ck_courses_one_home. Spaces were removed to simplify the
    platform, so the second home went and the constraint became a plain NOT NULL.
    If spaces return, the check returns with them: the moment a row can belong to
    both containers, "who can see this?" becomes a join across two membership
    tables and stops having a single answer — which is the property that makes
    access here auditable at all.

    CONTAINER MEMBERSHIP IS THE GRANT. Being in the org is what opens its
    courses — there is no per-course permission on top. `visibility` used to
    admit a third value, 'granted', meaning "only the accounts named by an
    AccessGrant"; it was a second access mechanism layered over the membership
    that had already decided who was in. Narrowing a course to SOME of an org's
    people is, for now, not expressible — that was what putting it in a space
    was for.

    What is left is not really a visibility scale:

        'org'      this container's course. Its members open it.
        'catalog'  platform courseware, visible to EVERY org and gated by
                   required_plan rather than by membership. Only super admins
                   may set it (routers/courses._assert_may_publish), which is
                   what makes the arm one-directional.
    """
    __tablename__ = 'courses'

    id = Column(Integer, primary_key=True, index=True)
    # The only home a course has. Was nullable while space_id was the other one.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    course_key = Column(String(64), nullable=False)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    sort_order = Column(Integer, nullable=False, server_default='0')
    visibility = Column(String(20), nullable=False, server_default='org')
    # Which self-serve plan opens this course: 'free' | 'premium'. Meaningful
    # on a CATALOG course, where it is the whole gate; on a container's own
    # course it is inert, because whoever enabled it has already decided who
    # may open it by deciding who is in the container.
    required_plan = Column(String(20), nullable=False, server_default='free')
    # Attribution, never tenancy.
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='SET NULL'),
                        nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'course_key', name='uq_course_org_key'),
        CheckConstraint("visibility IN ('org','catalog')",
                        name='ck_courses_visibility'),
        CheckConstraint("required_plan IN ('free','premium')",
                        name='ck_courses_required_plan'),
    )

    def __repr__(self):
        return f'<Course {self.course_key} org={self.org_id}>'


class VectorStore(Base):
    """
    A knowledge base (Pinecone index) owned by an account, with EXPLICIT
    credential bindings.

    Previously a knowledge base had no row of its own — it was just
    (owner_account_id, index_name) and its Pinecone/GCS credentials were resolved
    from the owner's account defaults at runtime. This row gives those credentials
    an explicit home so they don't drift when the owner changes a default:

      - pinecone_credential_id: which Pinecone key reaches this index.
      - gcs_credential_id: which GCS credential/bucket holds this index's source
        files (for storage at ingest and signed-URL retrieval).

    Both FKs are NULLABLE and ON DELETE SET NULL: a null binding (e.g. a row
    backfilled for a pre-existing index, or one whose bound credential was
    deleted) falls back to the owner's default for that type — see
    services/vector_store_credentials.py. New stores should set them explicitly.
    """
    __tablename__ = 'vector_stores'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    # Intra-org visibility. 'private' = creator plus explicit grantees;
    # 'org' = every member. Defaults to 'private' so widening is always a
    # deliberate act.
    visibility = Column(String(20), nullable=False, server_default='private')

    id = Column(Integer, primary_key=True, index=True)
    owner_account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    index_name = Column(String, nullable=False, index=True)
    display_name = Column(String(255), nullable=True)
    pinecone_credential_id = Column(Integer, ForeignKey('credentials.id', ondelete='SET NULL'), nullable=True, index=True)
    gcs_credential_id = Column(Integer, ForeignKey('credentials.id', ondelete='SET NULL'), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('owner_account_id', 'index_name', name='uq_vector_store_owner_index'),
    )

    owner = relationship('Account', foreign_keys=[owner_account_id])
    pinecone_credential = relationship('Credential', foreign_keys=[pinecone_credential_id])
    gcs_credential = relationship('Credential', foreign_keys=[gcs_credential_id])

    def __repr__(self):
        return f'<VectorStore owner={self.owner_account_id} index={self.index_name}>'


class AccessGrant(Base):
    """
    Unified access grant: a PRINCIPAL is granted a ROLE on a RESOURCE.

    Single source of truth for sharing, replacing the per-resource grant tables
    (AgentAccessGrant / VectorStoreAccessGrant / CredentialAccessGrant). One model
    means one resolver (services/access.py) and one audit query.

    ONE NAMED PERSON, INSIDE ONE ORG. That is the whole of what this table can
    express, and the narrowness is the point.

    `principal_type` used to admit 'group' as well, so a grant could name an
    access group. Groups became spaces, whose audience deliberately crossed org
    boundaries — which this table's org confinement (below, and in
    accessible_resource_ids) may not. Rather than teach the most
    security-sensitive filter in the codebase an exception, that cross-org arm
    lived in its own table, space_resources, where "this can be read from
    another org" was visible in the schema instead of hidden in a predicate.

    Spaces were removed to simplify the platform, so this table is now the ONLY
    way to reach somebody else's resource, and it never crosses an org:

        access_grants     a person, inside this org        (never crosses)

    Whatever restores cross-org reach belongs in a table of its own again, for
    the reason above. Do not add a "this grant may cross" column here.

    - principal_type/principal_id: 'account' + the account's id.
    - resource_type/resource_id: 'agent' | 'vector_store' | 'credential' +
      row id.
    - role: 'read' | 'write' | 'use'. Interpreted per resource type — vector_store
      uses read/write; agent and credential use 'use'. (For credentials 'use' means
      use server-side, never view the plaintext.)

    Polymorphic by (type, id) columns rather than FKs so uniqueness/audit stay
    simple; resource/principal deletion is cleaned up app-side via
    access.revoke_grants_for_resource / revoke_grants_for_principal.
    """
    __tablename__ = 'access_grants'

    id = Column(Integer, primary_key=True, index=True)
    # The org this grant lives in. A grant may NEVER cross orgs: principal and
    # resource must both belong to this org, validated on every write. Without
    # that constraint a grant between two accounts who end up in different orgs
    # is a live cross-tenant read path.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=True, index=True)
    principal_type = Column(String(20), nullable=False)   # 'account' — see above
    principal_id = Column(Integer, nullable=False)
    resource_type = Column(String(20), nullable=False)    # 'agent' | 'vector_store' | 'credential'
    resource_id = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False, server_default='read')  # 'read' | 'write' | 'use'
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('principal_type', 'principal_id', 'resource_type', 'resource_id',
                         name='uq_access_grant_principal_resource'),
        CheckConstraint("principal_type IN ('account')", name='ck_access_grant_principal_type'),
        # NO 'course'. A grant names a RESOURCE — something that carries
        # credentials, quotas or content of its own. A course is an
        # ENABLEMENT, opened by being in the container that holds it, and the
        # per-course grant that briefly existed alongside that was a second
        # answer to a question the container had already answered.
        CheckConstraint("resource_type IN ('agent','vector_store','credential')", name='ck_access_grant_resource_type'),
        CheckConstraint("role IN ('read','write','use')", name='ck_access_grant_role'),
        Index('ix_access_grants_resource', 'resource_type', 'resource_id'),
        Index('ix_access_grants_principal', 'principal_type', 'principal_id'),
    )

    def __repr__(self):
        return (f'<AccessGrant {self.principal_type}={self.principal_id} '
                f'{self.role} {self.resource_type}={self.resource_id}>')


class AccessGrantEvent(Base):
    """
    Append-only audit log of access-grant changes: who granted/revoked/changed
    access to what, and when. Distinct from access_grants (the live state) — this
    survives revocation (which deletes the grant row) and role changes.

    Human-readable context is SNAPSHOTTED at event time (actor_email,
    principal_label, resource_label) so the log stays readable even after the
    actor/principal/resource is renamed or deleted. For that reason the id columns
    intentionally have NO foreign keys — the log is independent and immutable.
    """
    __tablename__ = 'access_grant_events'

    id = Column(Integer, primary_key=True, index=True)
    # Which org the event happened in. No FK cascade concerns here beyond the
    # column itself — like the id columns below, this log is deliberately
    # independent so it survives what it describes.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=True, index=True)
    # See services/audit.py for the vocabulary. Widened well past resource
    # grants: membership, tier, ceiling, publishing and super-admin-join events all
    # land here, so there is ONE chronological answer to "what happened in this
    # org, and who did it".
    event_type = Column(String(40), nullable=False, index=True)

    resource_type = Column(String(20), nullable=False)   # agent | vector_store | credential
    resource_id = Column(Integer, nullable=False)
    resource_label = Column(String(512), nullable=True)  # snapshot

    # Nullable: org-level events (a ceiling change, a suspension) act on the
    # organization itself and have no counterparty.
    principal_type = Column(String(20), nullable=True)   # account | group
    principal_id = Column(Integer, nullable=True)
    principal_label = Column(String(512), nullable=True)  # snapshot (group name / email)

    role = Column(String(20), nullable=True)             # role involved (new role for role_change)
    # Free text for events that are not about roles: what a ceiling or a
    # tier's module set BECAME. Unbounded on purpose — a module list is
    # longer than any role name, and squeezing it into `role` is what broke
    # the first ceiling change.
    detail = Column(Text, nullable=True)

    actor_account_id = Column(Integer, nullable=True, index=True)   # who performed the change
    actor_email = Column(String(320), nullable=True)               # snapshot

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)

    __table_args__ = (
        # Kept in step with services/audit.py AND with the migrations that
        # widen this constraint. It had already fallen behind by one verb
        # ('org.rename', added in a5b6c7d8e9f0): harmless against a migrated
        # database, but tests/conftest.py builds its schema from this metadata,
        # so a stale list here is a constraint violation that only appears on a
        # freshly created test database.
        CheckConstraint(
            "event_type IN ('create','revoke','role_change',"
            "'member.add','member.remove',"
            # member.tier_change is RETAINED for rows already written under it,
            # the same reason the space.* values below are. Roles replaced
            # tiers; the log does not relabel what already happened.
            "'member.tier_change','member.role_change',"
            "'org.create','org.suspend','org.restore','org.ceiling_change',"
            "'org.rename',"
            "'tier.modules_change',"
            "'catalog.publish','catalog.unpublish','catalog.grant','catalog.revoke',"
            "'super_admin.join',"
            # RETAINED DELIBERATELY. Nothing writes a space.* event any more —
            # spaces were removed — but rows recording that they once happened
            # are still in this table, and narrowing the constraint would both
            # fail to apply and quietly assert those events never occurred. An
            # audit log that rewrites its own history when a feature is deleted
            # is not an audit log. See services/audit.py.
            "'space.create','space.archive',"
            "'space.member_add','space.member_remove',"
            "'space.request','space.request_approve','space.request_deny',"
            "'space.resource_add','space.resource_remove')",
            name='ck_access_grant_event_type'),
        Index('ix_access_grant_events_resource', 'resource_type', 'resource_id'),
    )

    def __repr__(self):
        return (f'<AccessGrantEvent {self.event_type} actor={self.actor_account_id} '
                f'{self.principal_type}={self.principal_id} -> {self.resource_type}={self.resource_id}>')


class Company(Base):
    """
    Stores CRM company (organization) records.

    A Company belongs to an Account and groups together the Contacts that work
    there. The relationship is many-to-many via the CompanyContact join table:
    a Company has many Contacts, and a Contact can be associated with many
    Companies.
    """
    __tablename__ = 'companies'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    # Optional company details.
    domain = Column(String(255), nullable=True, index=True)   # e.g. "acme.com"
    website = Column(String(512), nullable=True)
    industry = Column(String(255), nullable=True)
    description = Column(Text, nullable=True)
    linkedin_url = Column(String(512), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='companies')
    # Join rows linking this company to its member contacts. Deleting the
    # company removes the associations (cascade) but never the contacts.
    contact_memberships = relationship('CompanyContact', back_populates='company', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<Company {self.id}: {self.name}>'


class CompanyContact(Base):
    """
    Join table associating a Company with a Contact (many-to-many).

    Mirrors the ContactListMember pattern: an explicit join row scoped to the
    owning Account, with a uniqueness constraint preventing duplicate links.
    """
    __tablename__ = 'company_contacts'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey('companies.id', ondelete='CASCADE'), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='CASCADE'), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    # Optional role/title the contact holds at this company, e.g. "CTO".
    # Per-association rather than per-contact since it varies by company.
    title = Column(String(255), nullable=True)

    added_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    company = relationship('Company', back_populates='contact_memberships')
    contact = relationship('Contact', back_populates='company_memberships')

    __table_args__ = (
        UniqueConstraint('company_id', 'contact_id', name='uq_company_contact'),
    )

    def __repr__(self):
        return f'<CompanyContact company={self.company_id} contact={self.contact_id}>'


class Contact(Base):
    """
    Stores CRM contact records.

    A Contact belongs to an Account and can have a chronological log of
    ContactEvents (calls, emails, meetings, notes, etc.).
    """
    __tablename__ = 'contacts'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    # Required fields
    first_name = Column(String(255), nullable=False)
    middle_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    # The default (primary) email. Kept named `email` for backward
    # compatibility — surfaced as "Default email" in the UI. Unique per
    # the existing constraint; alternates are not uniqueness-constrained.
    email = Column(String(255), nullable=False, unique=True, index=True)
    # Optional secondary emails. A contact often has two or three addresses
    # (work, personal, etc.). These are informational + searchable; outbound
    # campaigns still send only to `email`.
    alt_email_1 = Column(String(255), nullable=True)
    alt_email_2 = Column(String(255), nullable=True)

    # Optional contact details
    phone = Column(String(50), nullable=True)

    # Social media profiles. Stored as full profile URLs, one column per
    # platform — mirrors the flat one-per-contact pattern used by phone and
    # the alternate emails. All nullable; adding a platform later is a small
    # additive migration.
    linkedin_url = Column(String(512), nullable=True)
    instagram_url = Column(String(512), nullable=True)
    youtube_url = Column(String(512), nullable=True)
    x_url = Column(String(512), nullable=True)   # X (formerly Twitter)

    @hybrid_property
    def name(self) -> str:
        """Full display name combining first, middle, and last name."""
        parts = [self.first_name]
        if self.middle_name:
            parts.append(self.middle_name)
        if self.last_name:
            parts.append(self.last_name)
        return " ".join(parts)

    # CRM metadata
    source = Column(String(100), nullable=True)   # e.g. "website", "referral", "chat_bot", "import"

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='contacts')
    # Many-to-many with Company via the company_contacts join table. A contact
    # can be associated with multiple companies.
    company_memberships = relationship('CompanyContact', back_populates='contact', cascade='all, delete-orphan')
    events = relationship('ContactEvent', back_populates='contact', cascade='all, delete-orphan')
    career_timeline = relationship('CareerTimeline', back_populates='contact', cascade='all, delete-orphan')
    # No cascade: a deal outlives its contact (FK is ON DELETE SET NULL), so
    # deleting a contact preserves the deal with contact_id cleared.
    deals = relationship('Deal', back_populates='contact')
    list_memberships = relationship('ContactListMember', back_populates='contact', cascade='all, delete-orphan')
    email_events = relationship('EmailEvent', back_populates='contact')

    def __repr__(self):
        return f'<Contact {self.id}: {self.name}>'


class ContactList(Base):
    """
    A named subset of contacts for targeted outbound campaigns, sequences, etc.

    A ContactList belongs to an Account and holds references to a subset of that
    account's Contacts via the ContactListMember join table.
    """
    __tablename__ = 'contact_lists'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='contact_lists')
    members = relationship('ContactListMember', back_populates='contact_list', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<ContactList {self.id}: {self.name}>'


class ContactListMember(Base):
    """
    Join table linking a ContactList to its member Contacts.
    """
    __tablename__ = 'contact_list_members'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    contact_list_id = Column(Integer, ForeignKey('contact_lists.id', ondelete='CASCADE'), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='CASCADE'), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    added_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    contact_list = relationship('ContactList', back_populates='members')
    contact = relationship('Contact', back_populates='list_memberships')

    __table_args__ = (
        UniqueConstraint('contact_list_id', 'contact_id', name='uq_contact_list_member'),
    )

    def __repr__(self):
        return f'<ContactListMember list={self.contact_list_id} contact={self.contact_id}>'


class ContactEvent(Base):
    """
    Chronological log of interactions with a Contact.

    Each event captures what happened (event_type), a short title, optional
    description, and when it occurred (occurred_at supports backdating).
    """
    __tablename__ = 'contact_events'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='CASCADE'), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    # e.g. "note", "call", "email", "meeting", "demo", "proposal_sent", "contract_signed"
    event_type = Column(String(100), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    occurred_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    contact = relationship('Contact', back_populates='events')

    def __repr__(self):
        return f'<ContactEvent {self.id}: {self.event_type} for contact {self.contact_id}>'


class CareerTimeline(Base):
    """
    Tracks career history entries for a Contact.

    Each entry represents a role/position with a start date, optional end date,
    title, and optional description.
    """
    __tablename__ = 'career_timeline'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='CASCADE'), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    contact = relationship('Contact', back_populates='career_timeline')

    def __repr__(self):
        return f'<CareerTimeline {self.id}: {self.title} for contact {self.contact_id}>'


# Allowed pipeline stages for a Deal. Kept in code (not a DB enum) so the set
# can evolve without a migration; validated at the API boundary.
DEAL_STAGES = ('lead', 'qualified', 'proposal', 'negotiation', 'won', 'lost')


class Deal(Base):
    """
    A sales/CRM deal (opportunity).

    A Deal always belongs to an Account. The Contact link is OPTIONAL: a deal
    can exist before it's tied to a specific person, and if that contact is
    later deleted the deal is preserved with contact_id cleared (ON DELETE
    SET NULL) rather than cascade-deleted.
    """
    __tablename__ = 'deals'
    # Tenant scope. Reads filter on this; account_id below is retained as
    # created_by (attribution), never as the tenant key.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='SET NULL'), nullable=True, index=True)

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    # Monetary value of the deal. Numeric (not float) to avoid rounding drift.
    amount = Column(Numeric(14, 2), nullable=True)
    currency = Column(String(3), nullable=False, default='USD')

    # One of DEAL_STAGES; validated at the API layer.
    stage = Column(String(50), nullable=False, default='lead', index=True)

    expected_close_date = Column(Date, nullable=True)
    # Set when the deal is marked won/lost; left NULL while open.
    closed_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='deals')
    contact = relationship('Contact', back_populates='deals')

    @property
    def contact_name(self) -> str | None:
        """Display name of the linked contact, or None for account-level deals.

        List endpoints eager-load `contact` (joinedload) so reading this in a
        loop does not trigger N+1 queries.
        """
        return self.contact.name if self.contact else None

    def __repr__(self):
        return f'<Deal {self.id}: {self.title} ({self.stage})>'


class PendingToolApproval(Base):
    """
    Durable queue entry created when a HITL-gated tool (e.g. sendTxtEmailWithSes)
    wants to execute.  The record holds everything needed to act on an approval
    *without* persisting decrypted credentials — the credential_id in the
    payload is re-looked-up at execution time.

    Lifecycle: pending → approved (→ executed) | rejected | expired
    """
    __tablename__ = 'pending_tool_approvals'

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    agent_id = Column(Integer, ForeignKey('agents.id', ondelete='SET NULL'), nullable=True, index=True)
    chat_session_id = Column(Integer, ForeignKey('chat_sessions.id', ondelete='SET NULL'), nullable=True, index=True)

    tool_type = Column(String(100), nullable=False, index=True)
    status = Column(String(20), nullable=False, default='pending', index=True)

    # Stores tool arguments.  MUST NOT contain decrypted secrets.
    # For sendTxtEmailWithSes: {credential_id, to_email, subject, body}
    payload = Column(JSON, nullable=False)

    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='tool_approvals')
    email_events = relationship('EmailEvent', back_populates='tool_approval', cascade='all, delete-orphan')

    def __repr__(self):
        return f'<PendingToolApproval {self.id}: {self.tool_type} [{self.status}]>'


# PostgreSQL native enum type — mirrors the Alembic migration definition
_email_event_type_pg = PG_ENUM(
    'send', 'send_to_ses', 'delivery', 'open', 'bounce', 'complaint', 'click',
    'attempting', 'failed', 'other',
    name='emaileventtype',
    create_type=False,  # managed by Alembic, not SQLAlchemy metadata
)


class EmailEvent(Base):
    """
    Records a single event in the lifecycle of an email sent through Kalygo.

    One email send typically produces multiple events:
      attempting → send_to_ses → [send → delivery] → open (if tracking enabled)

    Or for failures:
      attempting → failed                    (our SendEmail call raised)
      send_to_ses → bounce / complaint        (SES accepted, then SNS reported)

    ``send_to_ses`` is *our* synchronous hand-off to SES (the SendEmail request).
    The bare ``send`` event — and ``delivery`` / ``bounce`` / ``complaint`` / ``click`` —
    are the asynchronous notifications emitted by the SES configuration set (via
    SNS); they are reserved for a future webhook and not written yet.

    message_id is the key for correlating those inbound SNS payloads back to a
    specific email record (it holds the SES MessageId from the hand-off).
    """
    __tablename__ = 'email_events'

    id = Column(Integer, primary_key=True, index=True)

    # Multi-tenant scoping — always filter by this first
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)

    # The HITL approval record that triggered the original send
    tool_approval_id = Column(Integer, ForeignKey('pending_tool_approvals.id', ondelete='SET NULL'), nullable=True, index=True)

    campaign_id = Column(Integer, ForeignKey('email_campaigns.id', ondelete='SET NULL'),
                         nullable=True, index=True)

    # Recipient — nullable to support group/campaign sends where there is no single primary recipient
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='SET NULL'), nullable=True, index=True)
    primary_recipient = Column(String(320), nullable=True)

    # Event classification
    event_type = Column(_email_event_type_pg, nullable=False, index=True)

    # Which credential was used to send (enables per-credential analytics)
    credential_id = Column(Integer, ForeignKey('credentials.id', ondelete='SET NULL'), nullable=True, index=True)
    # Domain portion of the sender address at send-time (e.g. "cmdlabs.io")
    sender_domain = Column(String(255), nullable=True, index=True)

    # Sending provider (ses | google_oauth | google_smtp)
    provider = Column(String(50), nullable=True)
    # Provider-assigned message ID — used to match inbound webhook notifications
    message_id = Column(String(255), nullable=True, index=True)

    # Arbitrary extra payload (bounce type/subtype, user-agent, IP, clicked URL, etc.)
    event_metadata = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='email_events')
    tool_approval = relationship('PendingToolApproval', back_populates='email_events')
    campaign = relationship('EmailCampaign')
    contact = relationship('Contact', back_populates='email_events')
    credential = relationship('Credential')

    def __repr__(self):
        return f'<EmailEvent {self.id}: {self.event_type} → {self.primary_recipient}>'


class EmailTemplate(Base):
    """
    A reusable, production-grade HTML email template with named variable slots.

    Templates use {{variable_name}} tokens in both subject_template and
    html_template.  The send_template_email_with_ses agent tool resolves those
    tokens at invocation time before queuing the rendered email for approval.

    The html_template MUST follow inbox-compatibility best practices:
    - Single-column, table-based layout, max-width 600 px
    - All CSS inline (no <style> blocks, no external sheets)
    - An open-tracking pixel is injected automatically at send time
    """
    __tablename__ = 'email_templates'

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    # Subject line — may contain {{variable}} tokens
    subject_template = Column(String(998), nullable=False)
    # Full production-grade HTML email body
    html_template = Column(Text, nullable=False)
    # Variable schema: [{"name": "first_name", "label": "First Name", "default": "there"}]
    variables = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(),
                        onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='email_templates')

    def __repr__(self):
        return f'<EmailTemplate {self.id}: {self.name}>'


_email_campaign_status_pg = PG_ENUM(
    'draft', 'active', 'paused', 'completed',
    name='emailcampaignstatus',
    create_type=False,
)


class EmailCampaign(Base):
    """
    A targeted email campaign that ties a template to a contact list.

    Each campaign gets a public-facing UUID for use in tracking links and
    external integrations.  The status column tracks the campaign lifecycle.
    """
    __tablename__ = 'email_campaigns'

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(UUID(as_uuid=True), default=uuid.uuid4, unique=True, nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    email_template_id = Column(Integer, ForeignKey('email_templates.id', ondelete='SET NULL'),
                               nullable=True, index=True)
    contact_list_id = Column(Integer, ForeignKey('contact_lists.id', ondelete='SET NULL'),
                             nullable=True, index=True)
    status = Column(_email_campaign_status_pg, nullable=False, default='draft', index=True)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(),
                        onupdate=func.now(), nullable=False)

    account = relationship('Account', back_populates='email_campaigns')
    email_template = relationship('EmailTemplate')
    contact_list = relationship('ContactList')
    ratings = relationship('EmailCampaignRating', back_populates='campaign')

    def __repr__(self):
        return f'<EmailCampaign {self.id}: {self.name}>'


class EmailCampaignRating(Base):
    """
    Stores a single star rating (1-5) submitted by an email recipient.

    Each row ties a rating to the campaign, template, and contact that
    produced it.  Uniqueness is enforced on tracking_id so that a
    recipient can only rate a given email once (first click wins).
    """
    __tablename__ = 'email_campaign_ratings'

    id = Column(Integer, primary_key=True, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    campaign_id = Column(Integer, ForeignKey('email_campaigns.id', ondelete='SET NULL'),
                         nullable=True, index=True)
    email_template_id = Column(Integer, ForeignKey('email_templates.id', ondelete='SET NULL'),
                               nullable=True, index=True)
    contact_id = Column(Integer, ForeignKey('contacts.id', ondelete='SET NULL'),
                        nullable=True, index=True)
    primary_recipient = Column(String(320), nullable=True)
    tracking_id = Column(String(255), nullable=False, unique=True, index=True)
    rating = Column(Integer, nullable=False)

    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    account = relationship('Account', back_populates='email_campaign_ratings')
    campaign = relationship('EmailCampaign', back_populates='ratings')
    email_template = relationship('EmailTemplate')
    contact = relationship('Contact')


# ---------------------------------------------------------------------------
# Model registration
# ---------------------------------------------------------------------------
# Imported for its SIDE EFFECT of attaching these tables to Base.metadata,
# not for any name it exports — hence the noqa. Alembic autogenerate would
# otherwise propose dropping them, and tests/conftest.py's create_all would
# not build them at all.
#
# Registering here rather than in each consumer means there is ONE place to
# get this right instead of one per entry point.
#
# Spaces — the platform's SECOND container, shared content whose members came
# from many orgs — used to be imported here from db/space_models.py. They were
# removed to simplify the platform. The separate module is worth recreating if
# they return: everything in THIS file is either tenant data or org-confined,
# and a container that belongs to no tenant should not be able to hide among it.
