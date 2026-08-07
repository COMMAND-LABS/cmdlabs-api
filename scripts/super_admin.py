"""
Grant or revoke platform super admin.

WHY THE VERB IS IN A FLAG AND NOT THE FILENAME
----------------------------------------------
This was grant_super_admin.py, and granting was all it could do — taking the
flag away meant hand-writing UPDATE against production, which is the one moment
you least want to be composing a WHERE clause. Revocation is the operation you
need to get right in a hurry: somebody leaves, or an account is compromised. It
deserves the same dry run, the same "matched no account" warning, and the same
printed list as granting.

So the verb is REQUIRED and explicit. `--apply --email x` on its own is an
error rather than a guess, because the two verbs are opposites and the cost of
picking the wrong one silently is somebody keeping access they should have lost.

WHAT HAPPENED TO sync_account_roles.py
--------------------------------------
That script existed to drag `accounts.role` back into agreement with Stripe,
because free/premium was a CACHE of subscription_status and caches drift. Plans
are now derived per request (config/plans_registry.plan_for_account), so there
is nothing to reconcile and that half of the script deleted itself. What is
left is the half that was never about billing at all.

ONE THING NOW
-------------
Granting used to do two things: set `is_super_admin` AND place the account in
the platform org — because entitlement resolved from whichever org the caller
was acting in, so a super admin sitting in their own workspace could browse
every org and not open Contacts. The placement was the fix for that.

It is no longer needed. Super admins bypass the module ceiling outright
(services/modules.effective_modules), so they work from wherever they already
are. The flag is one boolean, and revoking it is therefore complete: there is
no membership left behind to clean up, and nothing else to put back.

DELIBERATELY A SCRIPT, NOT AN ENDPOINT
--------------------------------------
No API path reads or writes this either way. A compromised account cannot
escalate itself, and there is no "make super admin" button to click by
accident.

Usage (from the repo root):
    python -m scripts.super_admin --grant  --email tad@cmdlabs.io   # dry run
    python -m scripts.super_admin --apply --grant  --email tad@cmdlabs.io
    python -m scripts.super_admin --apply --revoke --email tad@cmdlabs.io
    python -m scripts.super_admin --apply --grant --email a@x.io --email b@x.io
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from src.db.database import SessionLocal
from src.db.models import Account


def set_super_admin(dry_run: bool, emails: list[str], *, value: bool,
                    db=None) -> int:
    """Set is_super_admin to `value` on the named accounts.

    Returns the number of accounts actually CHANGED — granting somebody who is
    already a super admin counts zero, and so does revoking somebody who is not
    one. That makes both directions idempotent and makes the return value
    usable as "did anything happen".

    `db` lets a caller inject a session. The default opens one against
    POSTGRES_URL, which is what the CLI wants; tests pass their own so this can
    run without the production engine's SSL requirement.
    """
    verb = "granted" if value else "revoked"
    owns_session = db is None
    db = db or SessionLocal()
    wanted = {e.strip().lower() for e in emails if e.strip()}

    try:
        accounts = db.query(Account).order_by(Account.id).all()
        print(f"Read {len(accounts)} accounts.\n")

        # Decided first, WRITTEN second. A dry run that mutated the objects and
        # relied on somebody else to roll back would be a dry run in name only —
        # and with an injected session there is nobody to roll it back.
        changing = []
        unmatched = set(wanted)
        for account in accounts:
            email = (account.email or "").strip().lower()
            if email in wanted:
                unmatched.discard(email)
                if account.is_super_admin != value:
                    changing.append((account.id, email, account))

        for email in sorted(unmatched):
            print(f"  WARNING: --email {email} matched no account")
        if unmatched:
            print()

        if changing:
            print(f"Super admin {'would be ' if dry_run else ''}{verb} "
                  f"({len(changing)}):")
            for account_id, email, _ in changing:
                print(f"  #{account_id:<6} {email}")
            print()
        else:
            print(f"Nothing to change — no account would be {verb}.\n")

        current = sum(1 for a in accounts if a.is_super_admin)
        remaining = current + len(changing) if value else current - len(changing)
        if not value and remaining == 0:
            # Not refused: this script can put it back, so it is not a lockout.
            # Said out loud because losing the last one means nobody can open
            # /api/admin until somebody with shell access notices.
            print("  WARNING: this leaves NO super admins. Nobody will be able "
                  "to open the admin surface until one is granted again.\n")

        if dry_run:
            print(f"\n[DRY RUN] Would have {verb} super admin on "
                  f"{len(changing)} account(s). Nothing written.")
            return len(changing)

        for _, _, account in changing:
            account.is_super_admin = value

        print(f"Super admin accounts: {remaining}")

        if owns_session:
            db.commit()
            print(f"\nDone — {len(changing)} account(s) {verb}.")
        else:
            db.flush()
            print(f"\nDone — {len(changing)} {verb} (caller commits).")
        return len(changing)

    except Exception as e:
        db.rollback()
        print(f"Failed: {e}")
        raise
    finally:
        if owns_session:
            db.close()


def grant(dry_run: bool, emails: list[str], db=None) -> int:
    """Make the named accounts super admins."""
    return set_super_admin(dry_run, emails, value=True, db=db)


def revoke(dry_run: bool, emails: list[str], db=None) -> int:
    """Take super admin away from the named accounts.

    Complete on its own: the flag is the whole of what being a super admin is,
    so there is no membership, tier or ceiling left over afterwards. The
    account keeps its own org and whatever plan it pays for, exactly as if it
    had never been granted.
    """
    return set_super_admin(dry_run, emails, value=False, db=db)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    verb = parser.add_mutually_exclusive_group(required=True)
    verb.add_argument("--grant", action="store_true",
                      help="Make the named accounts super admins.")
    verb.add_argument("--revoke", action="store_true",
                      help="Take super admin away from the named accounts.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True,
                      help="Report only. The default.")
    mode.add_argument("--apply", action="store_true",
                      help="Write the changes.")
    parser.add_argument("--email", action="append", default=[],
                        help="Act on this address. Repeatable.")
    args = parser.parse_args()

    set_super_admin(dry_run=not args.apply, emails=args.email,
                    value=args.grant)


if __name__ == "__main__":
    main()
