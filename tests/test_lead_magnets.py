"""
Lead magnet signup, delivery email, and admin stats.

Three properties are pinned here:

  - the public signup endpoint records every valid request and queues exactly
    one email, with the same 201 whether or not the email has asked before;
  - the email is rendered entirely from the code registry — every link's
    label and URL appears in both parts, nothing from the request does;
  - the stats endpoint is super-admin only (404 to everyone else, like the
    rest of /api/admin) and reports counts per slug, including 0 for a
    registered magnet nobody has requested yet.

SES is stubbed at the name the router imported (see the import_module note in
test_emails_send.py: the package re-exports `router`, so a dotted string would
hit the APIRouter, not the module).
"""
from datetime import datetime, timedelta, timezone
from importlib import import_module

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.config import lead_magnets_registry as registry
from src.db.lead_magnet_signups import LeadMagnetSignup
from src.db.models import Account, OrganizationMember
from src.main import app
from tests.conftest import make_token

lead_magnets_router = import_module("src.routers.lead_magnets.router")
sender_module = import_module("src.routers.lead_magnets.send_lead_magnet_email_ses")

SLUG = registry.LEAD_MAGNETS[0].slug
SIGNUP_URL = f"/api/lead-magnets/{SLUG}/signups"
STATS_URL = "/api/admin/lead-magnets/stats"


@pytest.fixture(autouse=True)
def sent(monkeypatch):
    """Replace the SES sender with a recorder; returns the list of calls."""
    calls = []

    def _fake_send(to_email: str, slug: str) -> None:
        calls.append({"to": to_email, "slug": slug})

    monkeypatch.setattr(lead_magnets_router, "send_lead_magnet_email_ses", _fake_send)
    return calls


# ---------------------------------------------------------------------------
# registry
# ---------------------------------------------------------------------------

def test_registry_slugs_are_well_formed_and_unique():
    slugs = registry.all_slugs()
    assert slugs, "the registry needs at least one magnet for the pages to exist"
    assert len(slugs) == len(set(slugs))
    for slug in slugs:
        assert registry.SLUG_PATTERN.match(slug), slug
    for magnet in registry.LEAD_MAGNETS:
        assert magnet.links, f"{magnet.slug} would send an empty email"
        for link in magnet.links:
            assert link.url.startswith("https://"), link.url


# ---------------------------------------------------------------------------
# public signup
# ---------------------------------------------------------------------------

async def test_signup_records_row_and_queues_email(client: AsyncClient, db: Session, sent):
    resp = await client.post(SIGNUP_URL, json={
        "email": "  Alex@Example.COM ",
        "attribution": {
            "utm_source": "linkedin",
            "landing_path": f"/resources/{SLUG}",
            "not_a_field": "dropped",
        },
    }, headers={"User-Agent": "pytest-agent"})
    assert resp.status_code == 201, resp.text
    assert resp.json() == {"status": "sent"}

    row = db.query(LeadMagnetSignup).filter(LeadMagnetSignup.slug == SLUG).one()
    assert row.email == "alex@example.com"
    assert row.attribution == {"utm_source": "linkedin", "landing_path": f"/resources/{SLUG}"}
    assert row.user_agent == "pytest-agent"

    assert sent == [{"to": "alex@example.com", "slug": SLUG}]


async def test_signup_without_attribution_stores_null(client: AsyncClient, db: Session):
    resp = await client.post(SIGNUP_URL, json={"email": "a@b.co"})
    assert resp.status_code == 201
    row = db.query(LeadMagnetSignup).one()
    assert row.attribution is None


async def test_unknown_slug_is_404_and_writes_nothing(client: AsyncClient, db: Session, sent):
    resp = await client.post("/api/lead-magnets/not-a-magnet/signups", json={"email": "a@b.co"})
    assert resp.status_code == 404
    assert db.query(LeadMagnetSignup).count() == 0
    assert sent == []


@pytest.mark.parametrize("email", ["not-an-email", "no-at.example.com", "two words@x.io", ""])
async def test_invalid_email_is_422(client: AsyncClient, db: Session, sent, email):
    resp = await client.post(SIGNUP_URL, json={"email": email})
    assert resp.status_code == 422
    assert db.query(LeadMagnetSignup).count() == 0
    assert sent == []


async def test_repeat_signup_is_201_again_and_resends(client: AsyncClient, db: Session, sent):
    for _ in range(2):
        resp = await client.post(SIGNUP_URL, json={"email": "again@example.com"})
        assert resp.status_code == 201
        assert resp.json() == {"status": "sent"}

    assert db.query(LeadMagnetSignup).count() == 2
    assert len(sent) == 2


# ---------------------------------------------------------------------------
# the email itself
# ---------------------------------------------------------------------------

class _RecordingSes:
    def __init__(self, sink):
        self.sink = sink

    def send_email(self, **kwargs):
        self.sink.append(kwargs)
        return {"MessageId": "msg-1"}


def test_email_renders_every_registry_link(monkeypatch):
    calls = []
    monkeypatch.setattr(sender_module.boto3, "client", lambda *a, **k: _RecordingSes(calls))

    sender_module.send_lead_magnet_email_ses("someone@example.com", SLUG)

    assert len(calls) == 1
    msg = calls[0]
    assert msg["Source"] == "noreply@cmdlabs.io"
    assert msg["Destination"] == {"ToAddresses": ["someone@example.com"]}

    magnet = registry.get(SLUG)
    assert msg["Message"]["Subject"]["Data"] == magnet.subject
    html_part = msg["Message"]["Body"]["Html"]["Data"]
    text_part = msg["Message"]["Body"]["Text"]["Data"]
    for link in magnet.links:
        assert link.url in html_part
        assert link.label in html_part
        assert link.url in text_part
        assert link.label in text_part
    assert f"/resources/{SLUG}" in html_part
    assert f"/resources/{SLUG}" in text_part


def test_email_failure_is_swallowed(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("ses down")
    monkeypatch.setattr(sender_module.boto3, "client", _boom)
    # Must not raise: the signup row is already committed by the time this runs.
    sender_module.send_lead_magnet_email_ses("someone@example.com", SLUG)


def test_email_for_unknown_slug_sends_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(sender_module.boto3, "client", lambda *a, **k: _RecordingSes(calls))
    sender_module.send_lead_magnet_email_ses("someone@example.com", "retired-slug")
    assert calls == []


# ---------------------------------------------------------------------------
# admin stats
# ---------------------------------------------------------------------------

@pytest.fixture()
def super_admin_account(db, test_org):
    account = Account(id=901, email="superadmin-lm@cmdlabs.io", is_super_admin=True,
                      default_org_id=test_org.id)
    db.add(account)
    db.flush()
    db.add(OrganizationMember(
        org_id=test_org.id, account_id=account.id,
        role="manager", granted_by="grant",
    ))
    db.flush()
    return account


@pytest.fixture()
async def super_admin_client(_override_db, super_admin_account) -> AsyncClient:
    token = make_token(email=super_admin_account.email, user_id=super_admin_account.id)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as ac:
        yield ac


def _seed(db: Session, slug: str, email: str, created_at: datetime | None = None) -> None:
    row = LeadMagnetSignup(slug=slug, email=email)
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.flush()


async def test_stats_requires_auth(client: AsyncClient):
    resp = await client.get(STATS_URL)
    assert resp.status_code == 401


async def test_stats_is_404_for_non_super_admin(authed_client: AsyncClient):
    resp = await authed_client.get(STATS_URL)
    assert resp.status_code == 404


async def test_stats_counts_signups_and_uniques(super_admin_client: AsyncClient, db: Session):
    _seed(db, SLUG, "one@example.com")
    _seed(db, SLUG, "one@example.com")      # same person again
    _seed(db, SLUG, "two@example.com")
    _seed(db, "retired-slug", "old@example.com")
    db.commit()

    resp = await super_admin_client.get(STATS_URL)
    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["rows"]}

    assert rows[SLUG]["signups"] == 3
    assert rows[SLUG]["unique_emails"] == 2
    assert rows[SLUG]["title"] == registry.get(SLUG).title
    assert rows[SLUG]["first_signup_at"] is not None
    assert rows[SLUG]["last_signup_at"] is not None

    # A slug with rows but no registry entry still reports, without a title.
    assert rows["retired-slug"]["signups"] == 1
    assert rows["retired-slug"]["title"] is None

    # Sorted by signups, most requested first.
    assert resp.json()["rows"][0]["slug"] == SLUG


async def test_stats_lists_registered_magnets_with_zero_signups(super_admin_client: AsyncClient):
    resp = await super_admin_client.get(STATS_URL)
    assert resp.status_code == 200
    rows = {r["slug"]: r for r in resp.json()["rows"]}
    for slug in registry.all_slugs():
        assert rows[slug]["signups"] == 0
        assert rows[slug]["unique_emails"] == 0
        assert rows[slug]["last_signup_at"] is None


async def test_stats_since_filter_excludes_old_rows(super_admin_client: AsyncClient, db: Session):
    now = datetime.now(timezone.utc)
    _seed(db, SLUG, "old@example.com", created_at=now - timedelta(days=30))
    _seed(db, SLUG, "new@example.com", created_at=now - timedelta(hours=1))
    db.commit()

    since = (now - timedelta(days=1)).isoformat()
    resp = await super_admin_client.get(STATS_URL, params={"since": since})
    assert resp.status_code == 200, resp.text
    rows = {r["slug"]: r for r in resp.json()["rows"]}
    assert rows[SLUG]["signups"] == 1
    assert rows[SLUG]["unique_emails"] == 1
    assert resp.json()["since"] is not None
