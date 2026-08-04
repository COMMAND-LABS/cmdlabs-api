from sqlalchemy import Column, Integer, String, ForeignKey, UUID, JSON, DateTime, Date, func, Double, Float, Numeric, Enum, Text, Boolean, UniqueConstraint, CheckConstraint, Index, text
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM, JSONB
from .database import Base
from .service_name import ServiceName
import datetime
import uuid

# Roles an Account can hold, ordered most → least privileged.
#
#   admin   - staff. The full CRM dashboard. Never billed, and never promoted
#             or demoted by billing.
#   premium - a paying subscriber on the Premium plan. Held for exactly as long
#             as Stripe reports an entitling subscription.
#   free    - signed up, not paying. The default for every new account.
#
# NOTE: distinct from AccessGroupMember.role ('admin' | 'member'), which is a
# per-group relationship and has nothing to do with billing.
#
# Enforced by a CHECK constraint (not a PG enum) so the set can evolve with a
# plain UPDATE to the constraint rather than an enum migration.
ROLE_ADMIN = 'admin'
ROLE_PREMIUM = 'premium'
ROLE_FREE = 'free'
ACCOUNT_ROLES = (ROLE_ADMIN, ROLE_PREMIUM, ROLE_FREE)
DEFAULT_ACCOUNT_ROLE = ROLE_FREE

# Stripe subscription statuses that mean "this account has paid and is entitled
# to the Premium features". Deliberately excludes past_due/unpaid/incomplete:
# an attached card that was never successfully charged is not a paying member.
ACTIVE_SUBSCRIPTION_STATUSES = ('active', 'trialing')


def role_for_subscription(subscription_status: str | None, current_role: str) -> str:
    """
    The role an account should hold given what Stripe last said about it.

    free <-> premium is derived from the subscription, never set by hand, so the
    two can never drift: one function decides it, the webhook and the backfill
    script both call it. Admins are staff and pass through untouched — billing
    must never be able to demote them.

    Cancellation is immediate rather than at period end, so a downgrade demotes
    on the same request — entitlement never waits on a later webhook.
    """
    if current_role == ROLE_ADMIN:
        return ROLE_ADMIN
    return ROLE_PREMIUM if subscription_status in ACTIVE_SUBSCRIPTION_STATUSES else ROLE_FREE


class Account(Base):
    __tablename__ = 'accounts'
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True)
    hashed_password = Column(String)
    reset_token = Column(String)
    stripe_customer_id = Column(String, nullable=True)
    newsletter_subscribed = Column(Boolean, default=False, nullable=False)
    # See ACCOUNT_ROLES. Never settable by the account holder: free <-> member
    # is derived from the subscription by role_for_subscription(), and admin is
    # granted out of band. Not settable through PUT /accounts/me either way.
    role = Column(String(20), nullable=False, default=DEFAULT_ACCOUNT_ROLE,
                  server_default=DEFAULT_ACCOUNT_ROLE, index=True)
    # Subscription state, owned entirely by the Stripe webhook — nothing else
    # writes these. Entitlement is read from subscription_status, never from
    # "the customer has a payment method attached".
    #
    # No CHECK constraint on the status: the vocabulary belongs to Stripe and a
    # status they add later must not start rejecting webhook writes.
    stripe_subscription_id = Column(String, nullable=True, index=True)
    subscription_status = Column(String(30), nullable=True, index=True)
    subscription_current_period_end = Column(DateTime(timezone=True), nullable=True)
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
    access_groups = relationship('AccessGroup', back_populates='owner', cascade='all, delete-orphan')
    group_memberships = relationship('AccessGroupMember', back_populates='account', cascade='all, delete-orphan')
    tool_approvals = relationship('PendingToolApproval', back_populates='account', cascade='all, delete-orphan')
    email_events = relationship('EmailEvent', back_populates='account', cascade='all, delete-orphan')
    email_templates = relationship('EmailTemplate', back_populates='account', cascade='all, delete-orphan')
    email_campaigns = relationship('EmailCampaign', back_populates='account', cascade='all, delete-orphan')
    email_campaign_ratings = relationship('EmailCampaignRating', back_populates='account', cascade='all, delete-orphan')

    __table_args__ = (
        CheckConstraint("role IN ('admin','premium','free')", name='ck_accounts_role'),
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

    `slug` is NULL for a personal workspace: it has no public page until its
    owner decides to create one. Immutable once set — it is the org's public
    identity, so an auto-generated one would be a defect the owner is stuck
    with. Postgres treats NULLs as distinct, so UNIQUE still holds.

    Root (slug 'root') is now purely the PLATFORM org: staff only, and the home
    of published catalog content. It is still an ordinary org — nothing works
    because of its id — it simply has staff in it.

    `granted_modules` is the ceiling: which modules this org may use at all.
    Bespoke per org, no plan table. For a PERSONAL org it is the whole
    entitlement, because its single member is its owner and an owner bypasses
    the tier layer — so this is the column billing raises and lowers. Tiers
    only start mattering once an org has someone in it who is not the owner.
    Resolution intersects at read time (services/modules.py), so lowering a
    ceiling takes effect on the very next request with no cascade.
    """
    __tablename__ = 'organizations'

    id = Column(Integer, primary_key=True, index=True)
    # No index=True: the UniqueConstraint below already creates a backing index
    # in Postgres, and declaring both makes autogenerate see permanent drift.
    slug = Column(String(64), nullable=True)
    name = Column(String(255), nullable=False)
    owner_account_id = Column(Integer, ForeignKey('accounts.id', ondelete='SET NULL'),
                              nullable=True, index=True)
    granted_modules = Column(JSONB, nullable=False, server_default='[]')
    # Who owns granted_modules. 'subscription' means the Stripe webhook writes
    # it; 'grant' means a human did and billing must never undo it. The same
    # asymmetry as OrganizationMember.granted_by, one level up — which is where
    # it has to live now that the ceiling is a personal org's entitlement.
    ceiling_managed_by = Column(String(20), nullable=False,
                                server_default='subscription')
    # 'read_only' when an org_owner subscription lapses: members keep reading
    # and exporting, writes are refused. Never deletion — losing a team's data
    # on a failed payment is not a recoverable mistake.
    status = Column(String(20), nullable=False, server_default='active')
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('slug', name='uq_organizations_slug'),
        CheckConstraint("status IN ('active','read_only')",
                        name='ck_organizations_status'),
        CheckConstraint("ceiling_managed_by IN ('subscription','grant')",
                        name='ck_org_ceiling_managed_by'),
    )

    owner = relationship('Account', foreign_keys=[owner_account_id])
    members = relationship('OrganizationMember', back_populates='org',
                           cascade='all, delete-orphan')
    tiers = relationship('OrganizationTier', back_populates='org',
                         cascade='all, delete-orphan')

    @property
    def is_personal(self) -> bool:
        """A workspace with no public page — one member, who owns it."""
        return self.slug is None

    def __repr__(self):
        return f'<Organization {self.id}: {self.slug or "(personal)"}>'


class OrganizationTier(Base):
    """
    A named bundle of modules, defined per org. This is the org owner's matrix.

    A tier is WHAT you get; a subscription is one way to GET it (see
    OrganizationMember.granted_by). Keeping those separate is what allows an
    owner to comp a client into a paid tier without a subscription, and what
    stops a tier dropdown from becoming a way to hand out paid features by
    accident — the tier names the bundle, billing names the entitlement.

    Distinct from an access group by cardinality: a member holds exactly ONE
    tier (what they paid for -> modules) but may belong to MANY access groups
    (how they are organized -> which resources).
    """
    __tablename__ = 'organization_tiers'

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    tier_key = Column(String(64), nullable=False)
    label = Column(String(255), nullable=False)
    modules = Column(JSONB, nullable=False, server_default='[]')
    # Set only when an org owner sells this tier through their own connected
    # Stripe account. Unused until that ships.
    stripe_price_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'tier_key', name='uq_org_tier_key'),
    )

    org = relationship('Organization', back_populates='tiers')

    def __repr__(self):
        return f'<OrganizationTier org={self.org_id} {self.tier_key}>'


class OrganizationMember(Base):
    """
    An account's membership in an org, carrying the tier it holds there.

    The same account may be a member of several orgs at different tiers — you
    can be premium inside an org that pays and free in your own.

    granted_by is the override that makes comping work:
      'subscription' - owned by the Stripe webhook; lapses when billing does.
      'grant'        - set by an owner; NEVER written by any webhook.

    is_owner is a bypass rather than a stored set of grants. An owner always
    reaches every module the org's ceiling allows, so a bad save in the matrix
    can never lock them out of the page that would undo it.
    """
    __tablename__ = 'organization_members'

    id = Column(Integer, primary_key=True, index=True)
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'),
                        nullable=False, index=True)
    tier_key = Column(String(64), nullable=False)
    granted_by = Column(String(20), nullable=False, server_default='grant')
    is_owner = Column(Boolean, nullable=False, server_default=text('false'))
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('org_id', 'account_id', name='uq_org_member'),
        CheckConstraint("granted_by IN ('subscription','grant')",
                        name='ck_org_member_granted_by'),
    )

    org = relationship('Organization', back_populates='members')
    account = relationship('Account', back_populates='org_memberships',
                           foreign_keys=[account_id])

    def __repr__(self):
        return (f'<OrganizationMember org={self.org_id} account={self.account_id} '
                f'tier={self.tier_key}>')


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


class AccessGroup(Base):
    """
    Named group owned by an account. The owner can add/remove members
    and other account holders (agent owners) can grant the group access
    to their agents.
    """
    __tablename__ = 'access_groups'
    
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    owner_account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    # Groups are intra-org. Nullable during the expand phase of the migration;
    # tightened once every row is backfilled.
    org_id = Column(Integer, ForeignKey('organizations.id', ondelete='CASCADE'),
                    nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)
    
    owner = relationship('Account', back_populates='access_groups')
    members = relationship('AccessGroupMember', back_populates='group', cascade='all, delete-orphan')
    # Grants TO this group live in access_grants (principal_type 'group'); see
    # services/access.py. Removed on group delete via access.revoke_grants_for_principal.

    def __repr__(self):
        return f'<AccessGroup {self.id}: {self.name}>'


class AccessGroupMember(Base):
    """
    Junction table linking accounts to access groups they are members of.
    Only the group owner can add/remove members.
    """
    __tablename__ = 'access_group_members'
    
    id = Column(Integer, primary_key=True, index=True)
    access_group_id = Column(Integer, ForeignKey('access_groups.id', ondelete='CASCADE'), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey('accounts.id', ondelete='CASCADE'), nullable=False, index=True)
    # 'admin' members can co-manage the group (add/remove members, rename, manage grants);
    # 'member' is a plain member. The group owner is tracked separately on AccessGroup and
    # is never an access_group_members row.
    role = Column(String(50), nullable=False, server_default='member')
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('access_group_id', 'account_id', name='uq_access_group_members_group_account'),
    )

    group = relationship('AccessGroup', back_populates='members')
    account = relationship('Account', back_populates='group_memberships')
    
    def __repr__(self):
        return f'<AccessGroupMember group={self.access_group_id} account={self.account_id}>'


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

    - principal_type/principal_id: 'account' (an individual) or 'group' (an access
      group). Individuals and groups are both first-class; expansion to accounts
      lives only in access.members_of.
    - resource_type/resource_id: 'agent' | 'vector_store' | 'credential' + row id.
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
    principal_type = Column(String(20), nullable=False)   # 'account' | 'group'
    principal_id = Column(Integer, nullable=False)
    resource_type = Column(String(20), nullable=False)    # 'agent' | 'vector_store' | 'credential'
    resource_id = Column(Integer, nullable=False)
    role = Column(String(20), nullable=False, server_default='read')  # 'read' | 'write' | 'use'
    created_at = Column(DateTime(timezone=True), default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint('principal_type', 'principal_id', 'resource_type', 'resource_id',
                         name='uq_access_grant_principal_resource'),
        CheckConstraint("principal_type IN ('account','group')", name='ck_access_grant_principal_type'),
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
    # grants: membership, tier, ceiling, publishing and staff-join events all
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
        CheckConstraint(
            "event_type IN ('create','revoke','role_change',"
            "'member.add','member.remove','member.tier_change',"
            "'org.create','org.suspend','org.restore','org.ceiling_change',"
            "'tier.modules_change',"
            "'catalog.publish','catalog.unpublish','catalog.grant','catalog.revoke',"
            "'staff.join')",
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
# Imported for its SIDE EFFECT of attaching the catalog tables to
# Base.metadata, not for any name it exports — hence the noqa.
#
# The catalog lives in its own module because it belongs to the PLATFORM
# rather than to any tenant, and keeping that boundary visible in the file
# layout is worth a little awkwardness. But every consumer of Base.metadata
# needs those tables: Alembic autogenerate would otherwise propose dropping
# them, and tests/conftest.py's create_all would not build them at all
# (which surfaces as "relation catalog_items does not exist" from any query
# that reaches the catalog arm).
#
# Registering here rather than in each consumer means there is ONE place to
# get this right instead of one per entry point.
from . import catalog_models  # noqa: F401,E402
