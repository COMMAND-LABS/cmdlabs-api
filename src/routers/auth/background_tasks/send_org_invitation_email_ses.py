"""
The invitation email.

WHAT THIS REPLACED, AND WHY IT IS A SEPARATE SENDER
---------------------------------------------------
Inviting somebody used to send send_login_code_email_ses — the ordinary
sign-in code. The recipient got eight digits, a ten-minute expiry, and a line
telling them to ignore the message if they had not requested it. Which they had
not. Nothing named the person who invited them, the organization, or the fact
that an invitation existed at all.

That was a message problem, not a credential problem, and it is why this is its
own sender rather than a parameter on that one. A sign-in code answers "prove
you read this inbox". An invitation answers "somebody wants you in their
workspace, here is who and here is what you get" — different subject, different
body, different reason to open it.

The link carries a token that names the invitation. It is not what authorizes
the join: accepting requires signing in as the invited address, so a forwarded
link shows a stranger the page and nothing else. See services/invitations.
"""
import html
import logging
import os

import boto3

logger = logging.getLogger(__name__)


def send_org_invitation_email_ses(
    to_email: str,
    org_name: str,
    inviter_email: str | None,
    role_label: str,
    accept_url: str,
    expires_in_days: int,
) -> None:
    # Everything below is attacker-influenced to some degree — an org name is
    # owner-editable free text and the inviter's address is whatever they
    # signed up with — so all of it is escaped before it reaches the HTML part.
    org = html.escape(org_name or "an organization")
    who = html.escape(inviter_email) if inviter_email else None
    role = html.escape(role_label)
    url = html.escape(accept_url, quote=True)

    invited_by = f"{who} invited you" if who else "You have been invited"
    subject = f"{inviter_email or 'Someone'} invited you to {org_name}"

    try:
        client = boto3.client(
            "ses",
            region_name=os.getenv("AWS_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_KEY"),
        )

        client.send_email(
            Source="noreply@cmdlabs.io",
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": subject},
                "Body": {
                    "Html": {
                        "Data": (
                            f"<p>{invited_by} to join "
                            f"<strong>{org}</strong> on COMMAND LABS as a "
                            f"{role}.</p>"
                            f"<p style='margin:24px 0'>"
                            f"<a href='{url}' "
                            f"style='background:#111827;color:#ffffff;"
                            f"padding:12px 24px;border-radius:9999px;"
                            f"text-decoration:none;font-weight:600'>"
                            f"View invitation</a></p>"
                            f"<p style='color:#6b7280;font-size:13px'>"
                            f"Or paste this link into your browser:<br>"
                            f"<span style='word-break:break-all'>{url}</span>"
                            f"</p>"
                            # Says outright that nothing has happened yet. The
                            # whole point of the invitation is that the
                            # membership is not created until they say yes.
                            f"<p style='color:#6b7280;font-size:13px'>"
                            f"You have not been added to anything yet — "
                            f"opening the link lets you accept or decline. "
                            f"This invitation expires in {expires_in_days} "
                            f"days.</p>"
                            f"<p style='color:#6b7280;font-size:13px'>"
                            f"If you were not expecting this, you can ignore "
                            f"this email and nothing will happen.</p>"
                        )
                    },
                    "Text": {
                        "Data": (
                            f"{inviter_email or 'Someone'} invited you to join "
                            f"{org_name} on COMMAND LABS as a {role_label}.\n\n"
                            f"View the invitation:\n{accept_url}\n\n"
                            f"You have not been added to anything yet — "
                            f"opening the link lets you accept or decline.\n"
                            f"This invitation expires in {expires_in_days} days.\n\n"
                            f"If you were not expecting this, you can ignore "
                            f"this email and nothing will happen."
                        )
                    },
                },
            },
        )
    except Exception:
        logger.exception(
            "[send_org_invitation_email_ses] Failed to send invitation to %s",
            to_email,
        )
