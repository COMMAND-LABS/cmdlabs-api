"""Tests for scripts/sync_account_roles.py.

The script owns no rules of its own — it applies role_for_subscription() in
bulk — so these cover that it hits every account, respects admins, and honours
--dry-run.
"""

from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session

from scripts.sync_account_roles import sync
from src.db.models import Account


@pytest.fixture()
def accounts(db: Session) -> dict[str, Account]:
    """One account per interesting state, all holding the pre-migration role."""
    rows = {
        # Only paid-tier because it used to be the default — should go free.
        "stale": Account(id=101, email="stale@example.com", role="premium"),
        # Genuinely paying — should stay premium.
        "paying": Account(
            id=102, email="paying@example.com", role="premium",
            subscription_status="active",
        ),
        # Card failed — not entitled.
        "lapsed": Account(
            id=103, email="lapsed@example.com", role="premium",
            subscription_status="past_due",
        ),
        # Staff with no subscription — must not be demoted.
        "staff": Account(id=104, email="staff@example.com", role="admin"),
        # Already correct — must not be counted as a change.
        "settled": Account(id=105, email="settled@example.com", role="free"),
    }
    for account in rows.values():
        db.add(account)
    db.flush()
    return rows


def _run(db: Session, **kwargs) -> int:
    """Run the script against the test session rather than a real one."""
    with patch("scripts.sync_account_roles.SessionLocal", return_value=db), \
         patch.object(db, "commit", db.flush), patch.object(db, "close", lambda: None):
        return sync(**kwargs)


def test_sync_applies_the_subscription_rule(db: Session, accounts):
    changed = _run(db, dry_run=False, make_admin=[])

    assert accounts["stale"].role == "free"
    assert accounts["paying"].role == "premium"
    assert accounts["lapsed"].role == "free"
    assert accounts["staff"].role == "admin"
    assert accounts["settled"].role == "free"
    # stale + lapsed only; the other three were already right.
    assert changed == 2


def test_sync_dry_run_writes_nothing(db: Session, accounts):
    with patch("scripts.sync_account_roles.SessionLocal", return_value=db), \
         patch.object(db, "rollback", lambda: None), \
         patch.object(db, "close", lambda: None):
        changed = sync(dry_run=True, make_admin=[])

    assert changed == 2
    # The objects are still dirty in the session, but nothing was committed —
    # expire them and the database still holds the original roles.
    db.expire_all()
    assert accounts["stale"].role == "premium"
    assert accounts["lapsed"].role == "premium"


def test_sync_grants_admin_and_never_demotes_it(db: Session, accounts):
    changed = _run(db, dry_run=False, make_admin=["Stale@Example.com"])

    # Case-insensitive match, and the subscription rule does not then demote
    # the account it just promoted.
    assert accounts["stale"].role == "admin"
    assert accounts["staff"].role == "admin"
    # stale (-> admin) + lapsed (-> free)
    assert changed == 2


def test_sync_is_idempotent(db: Session, accounts):
    _run(db, dry_run=False, make_admin=[])
    assert _run(db, dry_run=False, make_admin=[]) == 0


def test_sync_warns_about_unknown_admin_email(db: Session, accounts, capsys):
    _run(db, dry_run=False, make_admin=["nobody@example.com"])
    assert "matched no account" in capsys.readouterr().out
