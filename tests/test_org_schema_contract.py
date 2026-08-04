"""
Schema-level guards for tenancy.

`assert_org_isolated` checks routes that exist. This file checks the SHAPE of
the schema, so a table added six months from now cannot quietly skip org
scoping — the failure mode there is a new feature that is simply never
tenant-scoped, which no route test would catch because no route test would be
written for it.

Reflection-driven on purpose: it stays correct as models are added, without
anyone remembering to update a list.
"""
import pytest

from src.db.models import Base

# Tables that carry account_id but must NEVER be org-scoped, each with the
# reason. Keep this list short and justified — every entry suppresses a real
# signal.
ACCOUNT_SCOPED_BY_DESIGN = {
    # Identity and billing follow the PERSON, not the tenant. A human is the
    # same human in every org they belong to.
    "accounts": "the account itself",
    "logins": "authentication audit, per person",
    "usage_credits": "billing, per person",
    "api_keys": "a credential belonging to a person; org binding is separate",
    # Credentials are identity, not tenant data: a third-party secret belongs
    # to the person who owns it and stays usable across the orgs they join.
    "credentials": "portable bring-your-own-key; nullable org_id means shared",
    "credential_defaults": "a per-person, per-org selection",
}

# Tables that reach their tenant through a parent row rather than a column.
# Denormalising org_id onto a high-volume child buys nothing and creates a
# divergence risk — but it does mean the reflection test cannot protect them,
# so each needs its own route-level isolation test.
SCOPED_VIA_PARENT = {
    "chat_messages": "via chat_sessions.chat_session_id",
}

# Tables with no tenant at all.
NOT_TENANT_DATA = {
    "waitlist", "feedback", "json_schemas", "chat_history",
    "organizations", "organization_members", "organization_tiers",
    "alembic_version",
}


def _tables_with(column: str) -> set[str]:
    return {
        name for name, table in Base.metadata.tables.items()
        if column in table.columns
    }


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Expected red until org scoping finishes. 20 tables are still "
        "account-scoped; C1 moves 10 of them (the CRM cluster plus agents and "
        "vector_stores) and the remaining 10 follow. strict=True means this "
        "FAILS once every table is classified — that failure is the signal to "
        "delete this marker and let the guard start protecting the codebase."
    ),
)
def test_every_account_scoped_table_is_classified():
    """A new table with account_id must be deliberately classified.

    This is the guard that actually earns its keep: it fails on the PR that
    adds an unclassified tenant table, rather than months later when someone
    notices one customer can see another's rows.
    """
    classified = (
        set(ACCOUNT_SCOPED_BY_DESIGN)
        | set(SCOPED_VIA_PARENT)
        | NOT_TENANT_DATA
        | _tables_with("org_id")
    )
    unclassified = {
        t for t in _tables_with("account_id") | _tables_with("owner_account_id")
        if t not in classified
    }
    assert not unclassified, (
        "These tables carry account_id but are neither org-scoped nor listed as "
        "deliberately account-scoped:\n  "
        + "\n  ".join(sorted(unclassified))
        + "\n\nAdd org_id (and a route-level assert_org_isolated test), or add the "
          "table to ACCOUNT_SCOPED_BY_DESIGN / SCOPED_VIA_PARENT with a reason."
    )


def test_org_id_columns_are_indexed():
    """An unindexed org_id turns every tenant-scoped read into a seq scan.

    Cheap to get wrong (easy to omit on a new table) and invisible until a
    table grows, so assert it structurally.
    """
    unindexed = []
    for name, table in Base.metadata.tables.items():
        if "org_id" not in table.columns:
            continue
        col = table.columns["org_id"]
        indexed = col.index or any(
            "org_id" in [c.name for c in ix.columns] for ix in table.indexes
        )
        if not indexed:
            unindexed.append(name)
    assert not unindexed, f"org_id is not indexed on: {sorted(unindexed)}"


def test_org_id_columns_reference_organizations():
    """org_id must be a real FK, so a deleted org cannot leave orphan rows
    that a later org reusing the id would inherit."""
    bad = []
    for name, table in Base.metadata.tables.items():
        if "org_id" not in table.columns:
            continue
        targets = {fk.column.table.name for fk in table.columns["org_id"].foreign_keys}
        if "organizations" not in targets:
            bad.append(name)
    assert not bad, f"org_id lacks an FK to organizations on: {sorted(bad)}"


def test_exception_lists_name_real_tables():
    """Stops the exception lists from silently rotting as tables are renamed —
    a stale entry would suppress the guard for a table that no longer exists
    while leaving its replacement unprotected."""
    known = set(Base.metadata.tables)
    stale = {
        t for t in set(ACCOUNT_SCOPED_BY_DESIGN) | set(SCOPED_VIA_PARENT)
        if t not in known
    }
    assert not stale, f"Exception list names tables that no longer exist: {sorted(stale)}"
