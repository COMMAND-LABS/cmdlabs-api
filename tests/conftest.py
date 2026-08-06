"""
Shared test fixtures for the kalygo3-ai-api test suite.

Key design choices:
- POSTGRES_URL is FORCE-SET to the test database URL before any app imports.
  This guarantees tests can never accidentally touch production.
- Each test runs inside a DB transaction that is rolled back, so tests are
  fast and isolated with zero cleanup.
- Auth tokens are minted directly (no login round-trip needed per test).
- External services (Stripe, GCS, PubSub) are not called; dependencies are
  overridden where needed.
"""

import os

# --- FORCE-SET test environment BEFORE any application imports ---
# Uses POSTGRES_TEST_URL if provided, otherwise defaults to local test DB.
# Critically: this OVERWRITES any existing POSTGRES_URL to prevent
# accidental operations against production.
_TEST_DB_URL = os.environ.get(
    "POSTGRES_TEST_URL",
    "postgresql://test:test@cmdlabs-test-pg:5432/kalygo_test"
)
os.environ["POSTGRES_URL"] = _TEST_DB_URL
os.environ.setdefault("AUTH_SECRET_KEY", "test-secret-key-do-not-use-in-prod")
os.environ.setdefault("AUTH_ALGORITHM", "HS256")
os.environ.setdefault("COOKIE_DOMAIN", "localhost")
os.environ.setdefault("STRIPE_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("PINECONE_API_KEY", "fake-pinecone-key")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")
os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("SENDGRID_API_KEY", "fake")
os.environ.setdefault("EMBEDDINGS_API_URL", "http://localhost:9999")
os.environ.setdefault("KB_INGEST_SA", "{}")
os.environ.setdefault("GCS_BUCKET_NAME", "test-bucket")
os.environ.setdefault("PUBSUB_TOPIC", "test-topic")
os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("CREDENTIAL_ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0zMi1ieXRlcw==")

from datetime import timedelta, datetime, timezone
from typing import Generator

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, Session

from src.db.database import Base
from src.config.modules_registry import MODULE_KEYS
from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.deps import get_db
from src.main import app


# ---------------------------------------------------------------------------
# Database fixtures
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ["POSTGRES_URL"]

test_engine = create_engine(
    TEST_DATABASE_URL,
    pool_pre_ping=True,
    connect_args={"connect_timeout": 5},
)

TestSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


@pytest.fixture(scope="session", autouse=True)
def _setup_database():
    """Create all tables (and required PG enum types) once per test session.

    SAFETY: This fixture NEVER drops tables. Schema is additive only.
    Data isolation is handled by per-test transaction rollback.
    """
    # Guard: refuse to run if the URL looks like a production database
    if any(host in TEST_DATABASE_URL for host in ["supabase.co", "neon.tech", "rds.amazonaws.com"]):
        raise RuntimeError(
            f"REFUSING to run tests: POSTGRES_URL points to a production-like host.\n"
            f"  URL: {TEST_DATABASE_URL[:50]}...\n"
            f"  Set POSTGRES_TEST_URL to a local/disposable database."
        )

    with test_engine.connect() as conn:
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE api_key_status_enum AS ENUM ('active', 'revoked');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE operation_type_enum AS ENUM ('INGEST', 'DELETE', 'UPDATE');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE operation_status_enum AS ENUM ('SUCCESS', 'FAILED', 'PARTIAL', 'PENDING');
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE emaileventtype AS ENUM (
                    'send', 'send_to_ses', 'delivery', 'open',
                    'bounce', 'complaint', 'click',
                    'attempting', 'failed', 'other'
                );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE credential_type_enum AS ENUM (
                    'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'GOOGLE_GEMINI_API_KEY',
                    'PINECONE_API_KEY', 'ELEVENLABS_API_KEY', 'SUPABASE',
                    'AWS_SES', 'GOOGLE_OAUTH', 'GOOGLE_GMAIL_SMTP'
                );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.execute(text("""
            DO $$ BEGIN
                CREATE TYPE emailcampaignstatus AS ENUM (
                    'draft', 'active', 'paused', 'completed'
                );
            EXCEPTION WHEN duplicate_object THEN NULL;
            END $$;
        """))
        conn.commit()

    # The test database is a long-lived container, so the CREATE TYPE blocks
    # above are skipped (duplicate_object) once it exists — leaving any enum
    # created before new values were added permanently stale. Reconcile the
    # values the models require with idempotent ADD VALUE statements. These must
    # run outside a transaction block (Postgres restriction), hence AUTOCOMMIT.
    with test_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for value in ("attempting", "failed"):
            conn.execute(text(
                f"ALTER TYPE emaileventtype ADD VALUE IF NOT EXISTS '{value}'"
            ))

    Base.metadata.create_all(bind=test_engine)

    # Same staleness, one layer along: create_all adds MISSING TABLES and never
    # alters existing ones, so a CHECK constraint that has been widened in the
    # models still holds its old definition on a database created before the
    # change. That surfaces as a CheckViolation on a value the models consider
    # perfectly legal — a confusing failure that looks like a bug in the code
    # under test rather than in the fixture.
    #
    # Reconciled from Base.metadata rather than from a hardcoded list, so this
    # keeps working the next time a vocabulary grows. Only enumerated
    # constraints need it: they are the ones that widen.
    _reconcile_check_constraints(("access_grant_events",))

    yield
    # No teardown — tables are left in place for inspection and speed.


def _reconcile_check_constraints(table_names) -> None:
    """Drop and re-create each named table's CHECK constraints from the models."""
    from sqlalchemy.schema import CheckConstraint

    with test_engine.connect() as conn:
        for table_name in table_names:
            table = Base.metadata.tables.get(table_name)
            if table is None:
                continue
            for constraint in table.constraints:
                if not isinstance(constraint, CheckConstraint):
                    continue
                if not constraint.name:
                    continue
                conn.execute(text(
                    f'ALTER TABLE {table_name} '
                    f'DROP CONSTRAINT IF EXISTS {constraint.name}'
                ))
                conn.execute(text(
                    f'ALTER TABLE {table_name} ADD CONSTRAINT '
                    f'{constraint.name} CHECK ({constraint.sqltext})'
                ))
        conn.commit()


@pytest.fixture()
def db() -> Generator[Session, None, None]:
    """Provide a transactional DB session that rolls back after each test."""
    connection = test_engine.connect()
    transaction = connection.begin()
    session = TestSessionLocal(bind=connection)

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture()
def _override_db(db: Session):
    """Override the app's get_db dependency with the test session."""

    def _get_test_db():
        yield db

    app.dependency_overrides[get_db] = _get_test_db
    yield
    app.dependency_overrides.pop(get_db, None)


# ---------------------------------------------------------------------------
# Auth fixtures
# ---------------------------------------------------------------------------

SECRET_KEY = os.environ["AUTH_SECRET_KEY"]
ALGORITHM = os.environ["AUTH_ALGORITHM"]


def make_token(email: str = "test@example.com", user_id: int = 1, hours: int = 12) -> str:
    """Mint a valid JWT for testing."""
    payload = {
        "sub": email,
        "id": user_id,
        "exp": datetime.now(timezone.utc) + timedelta(hours=hours),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


# Every tenant row needs an org now that org_id is NOT NULL, and suites that
# seed models directly reference this id as a constant. Pinned rather than
# sequence-assigned because Postgres sequences do NOT roll back with the
# surrounding transaction — an auto-assigned id would drift upward from test
# to test and any constant would go stale after the first one.
ROOT_ORG_ID = 1


@pytest.fixture(autouse=True)
def test_org(db: Session) -> Organization:
    """The root organization, as migration e8f9a0b1c2d3 creates it.

    autouse because org_id is NOT NULL on ten tables: a test that seeds a
    Contact without having asked for an org would otherwise fail on a foreign
    key rather than on whatever it meant to assert.

    Stands in for the platform org. Since org-per-signup, every org means the
    same thing — one tenant, whose members all see its rows — so a suite that
    needs a second tenant simply makes another org (tests/org_isolation).
    """
    org = db.query(Organization).filter(Organization.slug == "root").first()
    if org is None:
        org = Organization(
            id=ROOT_ORG_ID,
            slug="root",
            name="CMD LABS",
            # Every module enabled. Module gating is enforced for real now, so
            # a fixture org with an empty ceiling would 404 every gated route
            # and every suite would be testing entitlement instead of its own
            # subject. Tests that care about gating set their own ceiling.
            granted_modules=list(MODULE_KEYS),
            status="active",
        )
        db.add(org)
        db.flush()
        for tier_key, label in (("free", "Free"), ("premium", "Premium"),
                                ("org_owner", "Org Owner")):
            db.add(OrganizationTier(org_id=org.id, tier_key=tier_key, label=label,
                                    modules=list(MODULE_KEYS)))
        db.flush()
        # An explicit id does NOT advance the sequence, so the next org created
        # without one would collide on the primary key. Tests that build a
        # second org (org_isolation.make_tenant) hit this immediately.
        db.execute(text(
            "SELECT setval('organizations_id_seq', "
            "GREATEST((SELECT COALESCE(MAX(id), 1) FROM organizations), 1))"
        ))
    return org


@pytest.fixture()
def test_account(db: Session, test_org: Organization) -> Account:
    """Insert a test account, placed in the root org like a real signup."""
    account = Account(id=1, email="test@example.com", default_org_id=test_org.id)
    db.add(account)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id,
        account_id=account.id,
        tier_key="free",
        granted_by="grant",
        is_owner=False,
    ))
    db.flush()
    return account


@pytest.fixture()
def auth_token(test_account: Account) -> str:
    """Return a valid JWT for the test account."""
    return make_token(email=test_account.email, user_id=test_account.id)


# ---------------------------------------------------------------------------
# HTTP client fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def client(_override_db) -> AsyncClient:
    """Unauthenticated async HTTP client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture()
async def authed_client(_override_db, auth_token: str) -> AsyncClient:
    """Authenticated async HTTP client (JWT in Authorization header)."""
    transport = ASGITransport(app=app)
    headers = {"Authorization": f"Bearer {auth_token}"}
    async with AsyncClient(transport=transport, base_url="http://test", headers=headers) as ac:
        yield ac
