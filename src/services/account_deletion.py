"""
Deleting an account without billing it, orphaning a team, or erasing a team's data.

A bare `db.delete(account)` got four things wrong:

  1. The Stripe subscription kept running. The customer went on being charged
     and every later webhook logged "unknown account".
  2. An org the account OWNED was left with owner_account_id NULL. It dropped
     to the free plan and nobody could invite, set roles or pay for it again.
  3. Rows the account had AUTHORED inside somebody else's org cascaded away
     with it. account_id on tenant rows is attribution, not ownership (see
     routers/organizations/members._remove), so a departing colleague's
     deletion wiped the team's contacts and deals.
  4. Several tables reference accounts with no ON DELETE rule (usage_credits,
     logins, ingestion logs, agents, skills), so for most real accounts the
     delete failed outright on a foreign key.

What happens now, in order:

  - Refuse while the account owns an org that has other members. Ownership has
    to be handed over first (PUT /api/organizations/{id}/owner), because an
    ownerless team is unrecoverable without a super admin.
  - Cancel the Stripe subscription. If Stripe refuses, stop: deleting the only
    record of a subscription that is still charging would be worse than failing.
  - Revoke every access grant the account holds.
  - Hand authored rows in other orgs to that org's owner, so the team keeps them.
  - Delete the orgs the account owns alone, which cascades their data.
  - Delete rows in tables with no ON DELETE rule, then the account itself.
"""
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.models import (
    Account,
    Agent,
    CareerTimeline,
    Company,
    CompanyContact,
    Contact,
    ContactEvent,
    ContactList,
    ContactListMember,
    Deal,
    Logins,
    Organization,
    OrganizationMember,
    Skill,
    UsageCredits,
    VectorDbIngestionLog,
    VectorStore,
)
from src.services import access

logger = logging.getLogger(__name__)

# Org-scoped tables whose account_id means "who wrote this". Reassigned to the
# org's owner rather than deleted.
_AUTHORED = (Contact, Company, CompanyContact, ContactList, ContactListMember,
             ContactEvent, CareerTimeline, Deal, Agent, Skill)

# Account-scoped tables with no ON DELETE rule in the schema. Without deleting
# these first the account delete fails on a foreign key.
_NO_ACTION = (UsageCredits, VectorDbIngestionLog, Logins)


class OwnsTeamError(Exception):
    """The account owns an org with other members in it."""

    def __init__(self, org_names: list[str]):
        self.org_names = org_names
        super().__init__(", ".join(org_names))


def teams_owned(db: Session, account_id: int) -> list[Organization]:
    """Orgs this account owns that contain at least one other member."""
    return (
        db.query(Organization)
        .filter(Organization.owner_account_id == account_id)
        .filter(
            db.query(OrganizationMember.id)
            .filter(OrganizationMember.org_id == Organization.id,
                    OrganizationMember.account_id != account_id)
            .exists()
        )
        .all()
    )


def _reassign_authored_rows(db: Session, account_id: int) -> None:
    """Give rows authored in orgs this account does not own to the org's owner."""
    for model in _AUTHORED:
        db.execute(text(
            f"UPDATE {model.__table__.name} AS t "
            "SET account_id = o.owner_account_id "
            "FROM organizations AS o "
            "WHERE t.org_id = o.id AND t.account_id = :me "
            "AND o.owner_account_id IS NOT NULL AND o.owner_account_id <> :me"
        ), {"me": account_id})

    # A knowledge base is unique per (owner, index_name). One that would
    # collide with an index the new owner already has is left to cascade.
    db.execute(text(
        f"UPDATE {VectorStore.__table__.name} AS t "
        "SET owner_account_id = o.owner_account_id "
        "FROM organizations AS o "
        "WHERE t.org_id = o.id AND t.owner_account_id = :me "
        "AND o.owner_account_id IS NOT NULL AND o.owner_account_id <> :me "
        f"AND NOT EXISTS (SELECT 1 FROM {VectorStore.__table__.name} AS v2 "
        "  WHERE v2.owner_account_id = o.owner_account_id "
        "  AND v2.index_name = t.index_name)"
    ), {"me": account_id})


def delete_account(db: Session, account: Account, cancel_subscription) -> None:
    """Delete `account` safely. Commits.

    `cancel_subscription(subscription_id)` is passed in so the router owns the
    Stripe client and tests can stub it. It must raise if Stripe refuses.

    Raises OwnsTeamError when the account still owns a team.
    """
    teams = teams_owned(db, account.id)
    if teams:
        raise OwnsTeamError([o.name for o in teams])

    if (account.stripe_subscription_id
            and account.subscription_status not in (None, "canceled",
                                                    "incomplete_expired")):
        cancel_subscription(account.stripe_subscription_id)

    access.revoke_grants_for_principal(db, "account", account.id)
    _reassign_authored_rows(db, account.id)

    # Only solo orgs are left in this list, because teams were refused above.
    owned_ids = [row[0] for row in
                 db.query(Organization.id)
                   .filter(Organization.owner_account_id == account.id).all()]
    if owned_ids:
        db.query(Organization).filter(Organization.id.in_(owned_ids)) \
          .delete(synchronize_session=False)

    for model in _NO_ACTION:
        db.query(model).filter(model.account_id == account.id) \
          .delete(synchronize_session=False)

    # The bulk statements above bypassed the session, so drop any collections
    # it loaded before them or the ORM cascade will act on rows that are gone.
    db.expire_all()
    db.delete(account)
    db.commit()
    logger.info("[ACCOUNT] deleted account %s (orgs removed: %s)",
                account.id, owned_ids)
