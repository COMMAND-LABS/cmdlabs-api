"""
Cross-tenant isolation harness.

This file is the safety net. We deliberately do NOT use Postgres row-level
security — the app connects as the table owner (BYPASSRLS), agent tools open
their own sessions after the request session closes, and under PgBouncer
transaction pooling a stray `SET` would itself be a cross-tenant read. That
decision is defensible, but it means nothing in the database stops a forgotten
`WHERE org_id = ...`.

These helpers are what replaces it. A forgotten filter has no symptom: no
error, no log line, just another tenant's rows rendering as if they were
yours. The only way to catch it is to assert its absence, per route.

Use `assert_org_isolated` on EVERY route whose reads move to org scoping.

IMPORTANT: any test using these helpers must request the `_override_db`
fixture. `client_for` builds its own AsyncClient, which reaches the app but
not the test database unless `get_db` has been overridden — without it the app
opens a real (SSL-requiring) connection and every request 503s, which would
otherwise look like a routing bug.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from httpx import ASGITransport, AsyncClient
from sqlalchemy.orm import Session

from src.config.modules_registry import MODULE_KEYS
from src.db.models import (
    Account,
    Organization,
    OrganizationMember,
    OrganizationTier,
)
from src.main import app
from tests.conftest import make_token


@dataclass
class Tenant:
    """One org plus a member of it, and a client authenticated as that member."""
    org: Organization
    account: Account

    @property
    def org_id(self) -> int:
        return self.org.id

    @property
    def account_id(self) -> int:
        return self.account.id


def make_tenant(
    db: Session,
    *,
    slug: str,
    account_id: int,
    email: str | None = None,
    data_scope: str | None = None,
    tier_key: str = "member",
    is_owner: bool = True,
) -> Tenant:
    """Create an org with one member.

    Calling twice with the same slug adds a second member to the SAME org,
    which is how a team is built here.

    `data_scope` is accepted and ignored. Orgs no longer have one: every
    account owns its own org, so the flag that once distinguished the shared
    root lobby from a real team has nothing left to distinguish (migrations
    e3f4a5b6c7d8 / f4a5b6c7d8e9). The parameter stays so the ~30 existing call
    sites still read correctly rather than churning in a security suite whose
    diffs should stay easy to review.
    """
    # `slug` is kept as the fixture's GROUPING KEY, not a column: orgs no
    # longer have slugs, and two calls with the same key still mean "the same
    # org, with a second member in it". Matched on the name it produces.
    org = db.query(Organization).filter(Organization.name == slug.title()).first()
    if org is None:
        org = Organization(
            name=slug.title(),
            # Fully enabled by default — see the note in conftest.test_org.
            granted_modules=list(MODULE_KEYS),
            # 'grant', so the list above is actually what this org gets. A
            # 'subscription' ceiling is DERIVED from the owner's plan and
            # ignores the stored column entirely (services.modules.ceiling_for),
            # which would quietly give every test tenant the free plan and make
            # isolation tests pass because nothing resolved at all.
            ceiling_managed_by="grant",
        )
        db.add(org)
        db.flush()

    # A non-owner resolves their modules through a tier, so the tier has to
    # exist or they would see nothing regardless of the ceiling.
    if not db.query(OrganizationTier).filter(
            OrganizationTier.org_id == org.id,
            OrganizationTier.tier_key == tier_key).first():
        db.add(OrganizationTier(org_id=org.id, tier_key=tier_key,
                                label=tier_key.title(),
                                modules=list(MODULE_KEYS)))
        db.flush()

    account = Account(id=account_id, email=email or f"{slug}-{account_id}@x.com",
                      default_org_id=org.id)
    db.add(account)
    db.flush()

    db.add(OrganizationMember(
        org_id=org.id, account_id=account.id,
        tier_key=tier_key, granted_by="grant", is_owner=is_owner,
    ))
    db.flush()

    org.owner_account_id = org.owner_account_id or account.id
    db.flush()
    return Tenant(org=org, account=account)


def client_for(tenant: Tenant) -> AsyncClient:
    """An authenticated client acting as this tenant's member."""
    token = make_token(email=tenant.account.email, user_id=tenant.account_id)
    return AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    )


def _extract_ids(payload: Any, key: str | None) -> set:
    """Pull row ids out of a response body, tolerating the shapes in use here.

    Routes return either a bare list or an envelope like
    {"contacts": [...], "total": n}. Both are unwrapped rather than requiring
    every caller to describe its own shape.
    """
    if isinstance(payload, dict):
        if key and key in payload:
            payload = payload[key]
        else:
            listy = [v for v in payload.values() if isinstance(v, list)]
            if len(listy) != 1:
                raise AssertionError(
                    f"Cannot find the row list in response keys {list(payload)}; "
                    f"pass collection_key=... to disambiguate."
                )
            payload = listy[0]
    if not isinstance(payload, list):
        raise AssertionError(f"Expected a list of rows, got {type(payload).__name__}")
    return {row["id"] for row in payload if isinstance(row, dict) and "id" in row}


async def assert_org_isolated(
    path: str,
    *,
    owner: Tenant,
    intruder: Tenant,
    owner_row_ids: Iterable[int],
    collection_key: str | None = None,
    seed_intruder: Callable[[], Iterable[int]] | None = None,
) -> None:
    """Assert `path` never leaks `owner`'s rows to `intruder`.

    Checks three things, because each fails differently:

      1. the owner CAN see their own rows — otherwise a route that returns
         nothing at all would pass an isolation test vacuously, which is the
         classic way this kind of test rots into a no-op;
      2. the intruder sees NONE of them;
      3. if the intruder has rows of their own, they still see exactly those —
         proving the filter scopes rather than simply blocking.

    A 4xx for the intruder counts as isolated: refusing the request is a
    stronger outcome than filtering it.
    """
    owner_row_ids = set(owner_row_ids)
    assert owner_row_ids, "assert_org_isolated needs at least one seeded row to be meaningful"

    async with client_for(owner) as c:
        resp = await c.get(path)
        assert resp.status_code == 200, f"owner could not read {path}: {resp.status_code} {resp.text}"
        visible = _extract_ids(resp.json(), collection_key)
    missing = owner_row_ids - visible
    assert not missing, (
        f"{path}: owner cannot see their own rows {sorted(missing)} — "
        f"the isolation assertion below would pass vacuously."
    )

    intruder_ids = set(seed_intruder() or ()) if seed_intruder else set()

    async with client_for(intruder) as c:
        resp = await c.get(path)

    if resp.status_code != 200:
        assert 400 <= resp.status_code < 500, (
            f"{path}: intruder got {resp.status_code}; expected rows or a 4xx"
        )
        return

    leaked = _extract_ids(resp.json(), collection_key) & owner_row_ids
    assert not leaked, (
        f"CROSS-TENANT LEAK at {path}: org {intruder.org_id} can see "
        f"row(s) {sorted(leaked)} belonging to org {owner.org_id}"
    )

    if intruder_ids:
        theirs = _extract_ids(resp.json(), collection_key)
        assert intruder_ids <= theirs, (
            f"{path}: intruder lost their OWN rows {sorted(intruder_ids - theirs)} — "
            f"the filter is blocking rather than scoping"
        )
