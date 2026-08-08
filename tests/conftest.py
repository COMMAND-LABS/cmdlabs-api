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
from pathlib import Path

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
from src.config import plans_registry as plans
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
    """Bring the test database to head, the same way production gets there.

    RUNS THE REAL MIGRATIONS. This used to be Base.metadata.create_all plus a
    growing pile of reconcilers, and the pile was the tell: create_all adds
    MISSING TABLES and never alters an existing one, so every schema change
    that was not a brand-new table left the test database silently behind. Over
    time that needed hand-written fix-ups for enum values, then for CHECK
    constraints, and it still could not handle an added or dropped COLUMN — the
    most common change of all.

    The failures it produced were the expensive kind: not "the schema is old",
    but a CheckViolation on a value the models consider legal, or a TypeError
    naming a column that no longer exists. Every one of them read like a bug in
    the code under test.

    Running alembic instead means the test schema is built by exactly the
    script that builds production, so a migration that would fail there fails
    here first. It is also self-healing: a stale container is brought forward
    on the next run rather than needing to be dropped by hand.

    SAFETY: the production-host guard below runs BEFORE anything touches the
    database, because this fixture now runs DDL rather than only adding tables.
    """
    if any(host in TEST_DATABASE_URL
           for host in ["supabase.co", "neon.tech", "rds.amazonaws.com"]):
        raise RuntimeError(
            f"REFUSING to run tests: POSTGRES_URL points to a production-like host.\n"
            f"  URL: {TEST_DATABASE_URL[:50]}...\n"
            f"  Set POSTGRES_TEST_URL to a local/disposable database."
        )

    from alembic import command
    from alembic.config import Config

    # env.py reads POSTGRES_URL, which the top of this file has already pinned
    # to the test database — so there is no second place the URL could come
    # from and no way for this to reach production.
    config = Config(str(Path(__file__).resolve().parent.parent / "alembic.ini"))
    config.set_main_option("script_location", "alembic")
    command.upgrade(config, "head")

    yield
    # No teardown — the schema is left in place for inspection and speed.


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
    """The first organization, in the state the suite needs it.

    autouse because org_id is NOT NULL on ten tables: a test that seeds a
    Contact without having asked for an org would otherwise fail on a foreign
    key rather than on whatever it meant to assert.

    ASSERTS ITS STATE RATHER THAN ASSUMING IT IS ABSENT. Since the schema is
    built by the real migrations, org 1 already exists — migration e8f9a0b1c2d3
    seeds it — with the ceiling and tiers that were current when that migration
    was written. Both are now years of module additions out of date, so a
    fixture that only ran `if org is None` silently handed every suite a
    narrow org and 404'd half the routes under test.

    An ORDINARY org that happens to be the first one. There is no platform org
    any more: super admins bypass the module ceiling wherever they are, and
    publishing became a Space (itself since removed), so nothing works because of this row's id or its
    name.
    """
    org = db.query(Organization).filter(Organization.id == ROOT_ORG_ID).first()
    if org is None:
        org = Organization(id=ROOT_ORG_ID, name="CMD LABS")
        db.add(org)
        db.flush()

    # PINNED TO PREMIUM. Module gating is enforced for real, so a fixture org
    # on the free plan would 404 most gated routes and every suite would be
    # testing entitlement instead of its own subject. Tests that care about
    # gating narrow their own TIER, which is the layer that narrows.
    #
    # Pinned rather than "give the owner an active subscription" because a pin
    # also keeps the fixture out of the read-only grace window, which is
    # billing's business and not every suite's.
    org.pinned_plan = plans.PLAN_PREMIUM

    existing = {
        t.tier_key: t
        for t in db.query(OrganizationTier).filter(
            OrganizationTier.org_id == org.id).all()
    }
    # The surviving vocabulary, matching what ensure_org_tiers() seeds and what
    # b3c4d5e6f7a8 left behind. 'free', 'premium' and 'org_owner' were seeded
    # here until then — the first two borrowed the PLAN axis's names and the
    # third named ownership, which is organizations.owner_account_id and not a
    # tier at all. Seeding them here kept the tests exercising a shape the
    # product no longer has.
    for tier_key, label in (("owner", "Owner"), ("member", "Member")):
        tier = existing.get(tier_key)
        if tier is None:
            db.add(OrganizationTier(org_id=org.id, tier_key=tier_key,
                                    label=label, modules=list(MODULE_KEYS)))
        else:
            tier.modules = list(MODULE_KEYS)
    db.flush()

    # An explicit id does NOT advance the sequence, so the next org created
    # without one would collide on the primary key. Tests that build a second
    # org (org_isolation.make_tenant) hit this immediately.
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
        # 'free' until b3c4d5e6f7a8 retired it. It was a PLAN name doing a
        # tier's job, and the fixture seeded it with every module — so a tier
        # called "free" quietly granted more than the premium plan sells. The
        # surviving 'member' tier is seeded the same way, so what this account
        # can open is unchanged; only the name it goes by is.
        tier_key="member",
        granted_by="grant",
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
