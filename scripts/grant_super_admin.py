"""
Make somebody a platform super admin.

WHAT HAPPENED TO sync_account_roles.py
--------------------------------------
That script existed to drag `accounts.role` back into agreement with Stripe,
because free/premium was a CACHE of subscription_status and caches drift. Plans
are now derived per request (config/plans_registry.plan_for_account), so there
is nothing to reconcile and that half of the script deleted itself. What is
left is the half that was never about billing at all: granting super admin.

ONE THING NOW
-------------
This used to do two things: grant `is_super_admin` AND place the account in the
platform org — because entitlement resolved from whichever org the caller was
acting in, so a super admin sitting in their own workspace could browse every
org and not open Contacts. The placement was the fix for that.

It is no longer needed. Super admins bypass the module ceiling outright
(services/modules.effective_modules), so they work from wherever they already
are. Granting it is now one boolean, and the platform org is one fewer thing
that has to exist.

DELIBERATELY A SCRIPT, NOT AN ENDPOINT
--------------------------------------
No API path grants super admin. A compromised account therefore cannot escalate
itself, and there is no "make super admin" button to click by accident.

Usage (from the repo root):
    python -m scripts.grant_super_admin --dry-run --email tad@cmdlabs.io
    python -m scripts.grant_super_admin --apply --email tad@cmdlabs.io
    python -m scripts.grant_super_admin --apply --email a@x.io --email b@x.io
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db.database import SessionLocal
from src.db.models import Account


def grant(dry_run: bool, emails: list[str], db=None) -> int:
    """Grant super admin to the named accounts.

    Returns the number of accounts newly granted super admin.

    `db` lets a caller inject a session. The default opens one against
    POSTGRES_URL, which is what the CLI wants; tests pass their own so this can
    run without the production engine's SSL requirement.
    """
    owns_session = db is None
    db = db or SessionLocal()
    wanted = {e.strip().lower() for e in emails if e.strip()}

    try:
        accounts = db.query(Account).order_by(Account.id).all()
        print(f"Read {len(accounts)} accounts.\n")

        # Decided first, WRITTEN second. A dry run that mutated the objects and
        # relied on somebody else to roll back would be a dry run in name only —
        # and with an injected session there is nobody to roll it back.
        granted = []
        unmatched = set(wanted)
        for account in accounts:
            email = (account.email or "").strip().lower()
            if email in wanted:
                unmatched.discard(email)
                if not account.is_super_admin:
                    granted.append((account.id, email, account))

        for email in sorted(unmatched):
            print(f"  WARNING: --email {email} matched no account")
        if unmatched:
            print()

        if granted:
            print(f"Super admin {'would be ' if dry_run else ''}granted "
                  f"({len(granted)}):")
            for account_id, email, _ in granted:
                print(f"  #{account_id:<6} {email}")
            print()
        else:
            print("No new super admin grants.\n")

        if dry_run:
            print(f"\n[DRY RUN] Would grant super admin to {len(granted)} "
                  f"account(s). Nothing written.")
            return len(granted)

        for _, _, account in granted:
            account.is_super_admin = True

        super_admin_total = sum(1 for a in accounts if a.is_super_admin)
        print(f"Super admin accounts: {super_admin_total}")

        if owns_session:
            db.commit()
            print(f"\nDone — {len(granted)} account(s) granted super admin.")
        else:
            db.flush()
            print(f"\nDone — {len(granted)} granted (caller commits).")
        return len(granted)

    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
        raise
    finally:
        if owns_session:
            db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Report only. The default.")
    mode.add_argument("--apply", action="store_true",
                      help="Write the changes.")
    parser.add_argument("--email", action="append", default=[],
                        help="Grant super admin to this address. Repeatable.")
    args = parser.parse_args()

    grant(dry_run=not args.apply, emails=args.email)


if __name__ == "__main__":
    main()
