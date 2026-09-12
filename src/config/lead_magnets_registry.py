"""
The lead magnet registry: what each public /resources page delivers by email.

A lead magnet is a free resource a visitor gets in exchange for their email.
Two things reference one:

  - the UI page (cmdlabs-ui/src/lib/lead-magnets.ts) — the marketing copy
  - the API (this file) — the email subject and the links that get sent

SLUGS ARE SHARED WITH cmdlabs-ui/src/lib/lead-magnets.ts. Keep them in step:
a UI page whose slug has no entry here gets a 404 from the signup endpoint,
which is deliberate — it is the only cross-repo check there is.

THE LINKS LIVE HERE AND NOWHERE ELSE. The signup request carries an email and
attribution, never content, so the server can never be talked into mailing an
arbitrary URL from noreply@cmdlabs.io. Adding a magnet is a code change on
purpose.
"""
import re
from dataclasses import dataclass, field

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True)
class LeadMagnetLink:
    label: str
    url: str
    # One line of context under the link, e.g. what the repo contains.
    note: str | None = None


@dataclass(frozen=True)
class LeadMagnet:
    slug: str
    # Shown in the admin stats and used as the Meta Lead content_name.
    title: str
    subject: str
    # One or two sentences above the list of links.
    intro: str
    links: tuple[LeadMagnetLink, ...] = field(default_factory=tuple)


LEAD_MAGNETS: tuple[LeadMagnet, ...] = (
    LeadMagnet(
        slug="post-to-social-media-with-ai-agents",
        title="How to Post to Social Media with AI Agents",
        subject="Your AI social media distribution prompt from COMMAND LABS",
        intro=(
            "Thanks for grabbing the guide. Below is the distribution prompt "
            "we use to have an AI agent write and post across social platforms, "
            "plus the repo it lives in — bookmark this email so you can come back to it."
        ),
        links=(
            LeadMagnetLink(
                label="The example distribution prompt",
                url=(
                    "https://github.com/COMMAND-LABS/"
                    "how-to-post-to-social-media-with-ai-agents/blob/main/"
                    "example_distribution_prompt.md"
                ),
                note="Copy it, swap in your brand and links, and hand it to your agent.",
            ),
            LeadMagnetLink(
                label="The full repo on GitHub",
                url="https://github.com/COMMAND-LABS/how-to-post-to-social-media-with-ai-agents",
                note="Setup, the walkthrough, and everything the prompt depends on.",
            ),
            LeadMagnetLink(
                label="COMMAND LABS on YouTube",
                url="https://youtube.com/@cmd_labs",
                note="Video walkthroughs of this and our other agent builds.",
            ),
            LeadMagnetLink(
                label="Book a free call",
                url="https://cal.com/cmdlabs",
                note="Want us to wire this into your own workflow? Grab a slot.",
            ),
        ),
    ),
    LeadMagnet(
        slug="automate-content-with-abc",
        title="Automate Content with Airtable, Blotato, and Claude",
        subject="Your ABC kit from COMMAND LABS: automate LinkedIn content with Airtable, Blotato, and Claude",
        intro=(
            "Here is the ABC kit. Set it up in this order: Claude first, then Blotato, "
            "then Airtable. After that, one prompt writes the week, you tick what you "
            "approve in Airtable, and one more prompt schedules it. Bookmark this email "
            "so you can come back to it."
        ),
        links=(
            LeadMagnetLink(
                label="The ABC kit on GitHub",
                url="https://github.com/COMMAND-LABS/automate-linkedin-content-with-abc",
                note="The setup guides, the example prompts, and the 1-click Airtable base install.",
            ),
            LeadMagnetLink(
                label="The 5-day campaign prompt",
                url=(
                    "https://github.com/COMMAND-LABS/automate-linkedin-content-with-abc/"
                    "blob/main/example_prompts/5_posts.compile.md"
                ),
                note="One prompt, five posts, each one moving the reader a stage closer to buying.",
            ),
            LeadMagnetLink(
                label="Book a free call",
                url="https://cal.com/cmdlabs",
                note="Want Claude running the rest of your operation, not just LinkedIn? Grab a slot.",
            ),
        ),
    ),
)

BY_SLUG = {m.slug: m for m in LEAD_MAGNETS}


def get(slug: str) -> LeadMagnet | None:
    return BY_SLUG.get(slug)


def all_slugs() -> list[str]:
    return [m.slug for m in LEAD_MAGNETS]
