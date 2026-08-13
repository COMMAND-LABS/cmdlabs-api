"""The code-defined, contact-scoped CRM agent.

This agent is NOT a database row and is NOT per-account. It is a single,
version-controlled config used by the dedicated contact-chat endpoint. Its
authority comes entirely from:

  - the session<->contact ownership gate (validated at session creation in
    kalygo3-ai-api), and
  - the structurally-scoped contact_crm tools (no contact-id parameter; every
    query filters by the caller's account_id and the bound contact_id).

It carries no credentials and exposes no contact-id parameter, so it is the
de-risked form of a "global" agent: a bad access decision still cannot reach
another account's or another contact's data.

Single source of truth for the contact-scoped tool type names lives here so
the fail-closed guard, the registry, and this config cannot drift.
"""

from typing import Any

# Tool type strings. Must match the ToolRegistry registrations in
# src/tools/__init__.py and the $defs in ai-api agent_config.v4.json.
CONTACT_SCOPED_TOOL_TYPES = frozenset(
    {"contactRead", "contactEventsRead", "contactEventWrite"}
)

# Display name only (the v4 schema's `data` does not allow a name field, so
# this is kept out of CONTACT_AGENT_CONFIG and used for the prompt's
# {agent_name} variable on the override path).
CONTACT_AGENT_NAME = "CRM Assistant"

CONTACT_AGENT_CONFIG: dict[str, Any] = {
    "schema": "agent_config",
    "version": 4,
    "data": {
        "systemPrompt": (
            "You are a CRM assistant scoped to a single contact. You can read "
            "this contact's details and activity, and log new activity events "
            "for them (calls, emails, meetings, notes). You are always and only "
            "operating on the one contact this conversation is about — you "
            "cannot access or reference any other contact. Be concise and "
            "confirm what you logged."
        ),
        "model": {"provider": "openai", "model": "gpt-4o-mini"},
        "tools": [
            {"type": "contactRead"},
            {"type": "contactEventsRead"},
            {"type": "contactEventWrite"},
        ],
    },
}


def contact_session_required(config_data: dict[str, Any]) -> bool:
    """True if the agent config declares any contact-scoped tool.

    Used by the fail-closed guard: if this is True but the chat session has no
    bound contact, the agent must refuse to run rather than run unscoped.
    Pure function (no I/O) so it is trivially unit-testable.
    """
    tools = config_data.get("tools") or []
    return any(
        isinstance(t, dict) and t.get("type") in CONTACT_SCOPED_TOOL_TYPES
        for t in tools
    )
