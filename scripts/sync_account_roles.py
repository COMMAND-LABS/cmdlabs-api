"""
Bring every account's `role` in line with what Stripe says about it.

Roles mean:

  * admin   - staff. Never touched by this script; billing must not be able to
              promote or demote staff. Grant it with --make-admin below.
  * premium - has an entitling Stripe subscription right now
              (subscription_status in 'active' / 'trialing')
  * free    - everyone else. The default for a new signup.

The rule lives in one place — role_for_subscription() in src/db/models.py —
and this script applies it in bulk. The Stripe webhook applies the same rule
per event, so after a successful run the two stay in agreement on their own.

When to run it:

  * Once, after the 'free' role migration, to demote the accounts that were
    only paid-tier because that used to be the default. (The migration already
    does this; running the script after is a safe no-op that reports the
    result, and is the way to fix things up if the migration was applied to a
    database that Stripe had since changed.)
  * Any time webhooks were missed — a bad signing secret, an endpoint that was
    down — and roles have drifted from reality.

Nothing here calls Stripe: it reads subscription_status, which the webhook
owns. If that column itself is stale, replay the events from the Stripe
dashboard first, then run this.

Usage (from the repo root):
    python -m scripts.sync_account_roles --dry-run      # report only, default-safe
    python -m scripts.sync_account_roles --apply
    python -m scripts.sync_account_roles --apply --make-admin tad@cmdlabs.io
    python -m scripts.sync_account_roles --dry-run --make-admin a@x.io --make-admin b@x.io
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db.database import SessionLocal
from src.db.models import (
    Account,
    ROLE_ADMIN,
    role_for_subscription,
)


def sync(dry_run: bool, make_admin: list[str]) -> int:
    """Returns the number of accounts whose role changed (or would change)."""
    db = SessionLocal()
    promote = {e.strip().lower() for e in make_admin if e.strip()}

    try:
        accounts = db.query(Account).order_by(Account.id).all()
        print(f"Read {len(accounts)} accounts.\n")

        # Admin grants first, so an account named on the command line is
        # already staff by the time the subscription rule runs and is left
        # alone by it rather than being demoted a moment later.
        promoted = []
        unmatched = set(promote)
        for account in accounts:
            email = (account.email or "").strip().lower()
            if email in promote:
                unmatched.discard(email)
                if account.role != ROLE_ADMIN:
                    promoted.append((account.id, email, account.role))
                    account.role = ROLE_ADMIN

        for email in sorted(unmatched):
            print(f"  WARNING: --make-admin {email} matched no account")
        if unmatched:
            print()

        changes = []
        for account in accounts:
            target = role_for_subscription(account.subscription_status, account.role)
            if target != account.role:
                changes.append((account.id, account.email, account.role, target,
                                account.subscription_status))
                account.role = target

        if promoted:
            print(f"Admin grants ({len(promoted)}):")
            for account_id, email, was in promoted:
                print(f"  #{account_id:<6} {email:<40} {was} -> admin")
            print()

        if changes:
            print(f"Role changes ({len(changes)}):")
            for account_id, email, was, now, status in changes:
                print(
                    f"  #{account_id:<6} {email or '(no email)':<40} "
                    f"{was} -> {now}   (subscription: {status or 'none'})"
                )
            print()
        else:
            print("No role changes needed — every account already agrees with Stripe.\n")

        # Report the resulting shape either way; it is the number worth eyeballing
        # before an --apply.
        tally: dict[str, int] = {}
        for account in accounts:
            tally[account.role] = tally.get(account.role, 0) + 1
        print("Resulting roles: " + ", ".join(
            f"{role}={count}" for role, count in sorted(tally.items())
        ))

        total = len(promoted) + len(changes)
        if dry_run:
            db.rollback()
            print(f"\n[DRY RUN] Would update {total} account(s). No changes written.")
        else:
            db.commit()
            print(f"\nDone — {total} account(s) updated.")
        return total

    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
        raise
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync account roles with their Stripe subscription state.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change and write nothing (the default).",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the changes.",
    )
    parser.add_argument(
        "--make-admin",
        action="append",
        default=[],
        metavar="EMAIL",
        help="Grant admin to this email (repeatable). Admins are never demoted.",
    )
    args = parser.parse_args()

    # Default to the safe mode when neither flag is given.
    dry_run = not args.apply
    if dry_run and not args.dry_run:
        print("No --apply given; running as --dry-run.\n")

    sync(dry_run=dry_run, make_admin=args.make_admin)


if __name__ == "__main__":
    main()
