"""Invitations become a thing that is accepted

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-08 21:10:00.000000

WHAT CHANGES
------------
A new table, organization_invitations, and four new audit event types. Nothing
existing is altered or dropped.

Adding somebody to an org used to write the OrganizationMember row immediately,
inside the invite request. From now on the invite writes a row here and the
membership is written when the invitee ACCEPTS.

WHY
---
The old behaviour was documented and defended in
routers/organizations/members.py, under a header explaining why an invite
needed no token: the platform authenticates by OTP, so reaching an org already
requires controlling the invitee's inbox, and a token would be a second secret
sent to the same place. That is still true. This table is not a security fix
and does not claim to be.

It is a consent fix, and the old header named the gap itself:

    "What that DOES cost is consent: an account that already exists is added
    immediately, without being asked. That is the right trade for colleagues
    you already work with and the wrong one for strangers, and it is the thing
    to revisit when orgs start inviting people they have not met."

This is that revisit.

It also fixes what the invitee actually received. Because there was no
invitation, there was no invitation email: an invite sent the ordinary sign-in
code — eight digits, no sender, no org name, no reason for the message to
exist. Correct as a credential and useless as a message.

EXISTING MEMBERSHIPS ARE UNTOUCHED
----------------------------------
Everybody already in an org stays in it. There is no backfill to write: an
invitation describes an offer that has not been answered, and every historical
invite was answered by construction the moment it was sent.

ON THE PARTIAL UNIQUE INDEX
---------------------------
uq_org_invitation_pending covers (org_id, email) WHERE the row is still live.
Uniqueness cannot be unconditional — the same person may be invited, leave, and
be invited again, and each of those is a row worth keeping. But two LIVE
invitations to one address is how somebody ends up clicking the dead token, so
re-inviting refreshes the existing row rather than adding a second.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Kept in step with services/audit.py and db/models.AccessGrantEvent. Copied
# rather than imported: a migration is a point-in-time snapshot and must not
# follow the app as it changes.
EVENT_TYPES = [
    'create', 'revoke', 'role_change',
    'member.add', 'member.remove',
    'member.tier_change', 'member.role_change',
    'member.invite', 'member.invite_revoke',
    'member.invite_decline', 'member.invite_resend',
    'org.create', 'org.suspend', 'org.restore', 'org.ceiling_change',
    'org.rename',
    'tier.modules_change',
    'catalog.publish', 'catalog.unpublish', 'catalog.grant', 'catalog.revoke',
    'super_admin.join',
    # Retained deliberately — spaces are gone, but rows recording that they
    # happened are not. See db/models.AccessGrantEvent.
    'space.create', 'space.archive',
    'space.member_add', 'space.member_remove',
    'space.request', 'space.request_approve', 'space.request_deny',
    'space.resource_add', 'space.resource_remove',
]

INVITE_EVENTS = {
    'member.invite', 'member.invite_revoke',
    'member.invite_decline', 'member.invite_resend',
}
PREVIOUS = [v for v in EVENT_TYPES if v not in INVITE_EVENTS]

PENDING = ('accepted_at IS NULL AND declined_at IS NULL '
           'AND revoked_at IS NULL')


def _check_sql(values) -> str:
    return "event_type IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    op.create_table(
        'organization_invitations',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), nullable=False),
        # 320 is the RFC maximum for an address, matching accounts.email.
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False,
                  server_default='community_member'),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('invited_by_account_id', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('declined_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['org_id'], ['organizations.id'],
                                ondelete='CASCADE'),
        # SET NULL, not CASCADE: deleting the inviter's account must not
        # silently withdraw an offer somebody was already sent.
        sa.ForeignKeyConstraint(['invited_by_account_id'], ['accounts.id'],
                                ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        # Mirrors ck_org_member_role. An invitation cannot promise a role a
        # membership could not hold — 'owner' above all, which is a column on
        # organizations and is not grantable.
        sa.CheckConstraint("role IN ('manager','community_member')",
                           name='ck_org_invitation_role'),
    )
    op.create_index('ix_organization_invitations_id',
                    'organization_invitations', ['id'])
    op.create_index('ix_organization_invitations_org_id',
                    'organization_invitations', ['org_id'])
    op.create_index('ix_organization_invitations_email',
                    'organization_invitations', ['email'])
    op.create_index('ix_organization_invitations_invited_by_account_id',
                    'organization_invitations', ['invited_by_account_id'])
    # Unique so accepting is one indexed read against a hash, the same shape as
    # any other credential lookup.
    op.create_index('ix_organization_invitations_token_hash',
                    'organization_invitations', ['token_hash'], unique=True)
    op.create_index('uq_org_invitation_pending', 'organization_invitations',
                    ['org_id', 'email'], unique=True,
                    postgresql_where=sa.text(PENDING))

    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(EVENT_TYPES))


def downgrade() -> None:
    # Live invitations are DROPPED, and that is the honest outcome: without the
    # table there is nothing for an accept link to resolve against, so leaving
    # the rows somewhere else would only produce tokens that resolve to
    # nothing. Anyone mid-invite is re-invited under the old immediate-add
    # behaviour, which is what a downgrade restores.
    op.drop_index('uq_org_invitation_pending',
                  table_name='organization_invitations')
    op.drop_index('ix_organization_invitations_token_hash',
                  table_name='organization_invitations')
    op.drop_index('ix_organization_invitations_invited_by_account_id',
                  table_name='organization_invitations')
    op.drop_index('ix_organization_invitations_email',
                  table_name='organization_invitations')
    op.drop_index('ix_organization_invitations_org_id',
                  table_name='organization_invitations')
    op.drop_index('ix_organization_invitations_id',
                  table_name='organization_invitations')
    op.drop_table('organization_invitations')

    # Collapse rather than delete, matching a5b6c7d8e9f0. An audit row is
    # evidence; dropping the entries that no longer fit the vocabulary would
    # make a downgrade a way to erase history.
    op.execute(
        "UPDATE access_grant_events SET event_type = 'member.add' "
        "WHERE event_type IN ('member.invite', 'member.invite_resend')"
    )
    op.execute(
        "UPDATE access_grant_events SET event_type = 'member.remove' "
        "WHERE event_type IN ('member.invite_revoke', 'member.invite_decline')"
    )
    op.drop_constraint('ck_access_grant_event_type', 'access_grant_events',
                       type_='check')
    op.create_check_constraint('ck_access_grant_event_type',
                               'access_grant_events', _check_sql(PREVIOUS))
