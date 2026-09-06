"""
The lead magnet delivery email. Runs as a FastAPI BackgroundTask.

Takes a slug rather than the content so the queued task carries nothing a
request could have influenced: the subject, intro and links are read from the
registry at send time. Best-effort and never raises — the signup row is
already committed by the time this runs, so a slow or failing SES call must
not turn a recorded signup into a failed request (same rule as the invitation
mail in services/invitation_mail.py).
"""
import html
import logging
import os

import boto3

from src.config import lead_magnets_registry as registry

logger = logging.getLogger(__name__)

# Same default as services/invitation_mail.py and for the same reason: the API
# decides its own outbound URLs.
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:3001")

FROM_ADDRESS = "noreply@cmdlabs.io"


def resource_url(slug: str) -> str:
    return f"{APP_BASE_URL}/resources/{slug}"


def render_html(magnet: registry.LeadMagnet) -> str:
    """The HTML part. Registry content is trusted, but escaped anyway — the
    discipline costs nothing and keeps the sender safe if the registry ever
    moves to a database."""
    items = []
    for link in magnet.links:
        url = html.escape(link.url, quote=True)
        label = html.escape(link.label)
        note = (
            f"<br><span style='color:#6b7280;font-size:13px'>{html.escape(link.note)}</span>"
            if link.note else ""
        )
        items.append(
            f"<li style='margin:0 0 14px'>"
            f"<a href='{url}' style='color:#165dfc;font-weight:600'>{label}</a>"
            f"{note}</li>"
        )
    page = html.escape(resource_url(magnet.slug), quote=True)
    return (
        f"<p>{html.escape(magnet.intro)}</p>"
        f"<ul style='padding-left:20px;margin:24px 0'>{''.join(items)}</ul>"
        f"<p>— The COMMAND LABS team</p>"
        f"<p style='color:#6b7280;font-size:13px'>"
        f"You're receiving this because you requested "
        f"<strong>{html.escape(magnet.title)}</strong> at "
        f"<a href='{page}' style='color:#6b7280'>{page}</a>.</p>"
    )


def render_text(magnet: registry.LeadMagnet) -> str:
    """The plain-text part: label, URL, and note per link."""
    lines = [magnet.intro, ""]
    for link in magnet.links:
        lines.append(f"- {link.label}: {link.url}")
        if link.note:
            lines.append(f"  {link.note}")
    lines += [
        "",
        "— The COMMAND LABS team",
        "",
        f"You're receiving this because you requested {magnet.title} at "
        f"{resource_url(magnet.slug)}.",
    ]
    return "\n".join(lines)


def send_lead_magnet_email_ses(to_email: str, slug: str) -> None:
    magnet = registry.get(slug)
    if magnet is None:
        # The endpoint validates the slug before queueing, so this is a
        # registry edit racing an in-flight request. Log and drop.
        logger.error("[send_lead_magnet_email_ses] Unknown slug %r for %s", slug, to_email)
        return

    try:
        client = boto3.client(
            "ses",
            region_name=os.getenv("AWS_REGION"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_KEY"),
        )
        client.send_email(
            Source=FROM_ADDRESS,
            Destination={"ToAddresses": [to_email]},
            Message={
                "Subject": {"Data": magnet.subject},
                "Body": {
                    "Html": {"Data": render_html(magnet)},
                    "Text": {"Data": render_text(magnet)},
                },
            },
        )
    except Exception:
        logger.exception(
            "[send_lead_magnet_email_ses] Failed to send %r to %s", slug, to_email
        )
