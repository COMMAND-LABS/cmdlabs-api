"""Contact-scoped CRM tools.

These tools are *structurally* scoped: none of them expose a contact-id (or
account-id) parameter, so the model literally cannot express "a different
contact". The bound contact_id and the caller's account_id are closed over
from the agent context and applied to every query. The account_id filter is
defense-in-depth (the session<->contact ownership gate already validated the
binding at creation; see contact_agent_config).

Transaction/concurrency: the request DB session is closed by
prepare_agent_context before the agent loop runs, so each tool opens its own
short-lived session (the same pattern as persist_ai_message), commits, and
closes — never touching the agent-loop session.

Testability: the session factory is injectable via the `session_factory`
kwarg (defaults to the app SessionLocal), so behavior can be unit-tested
against a disposable database without monkeypatching.

Extension point: contact-scoped Update/Delete tools would slot in here
symmetrically — e.g. update_contact_event(event_id, ...) would still
`.filter(ContactEvent.contact_id == contact_id, ...)` before mutating, so
even an event_id argument could not escape the bound contact.
"""

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from src.db.models import Contact, ContactEvent
from src.services.org_scope import tenant_predicate

from .db_read import serialize_value


def _default_session_factory():
    # Imported lazily so importing this module never triggers DB engine setup.
    from src.db.database import SessionLocal
    return SessionLocal()


def _resolve_session_factory(kwargs: dict[str, Any]) -> Callable[[], Any]:
    return kwargs.get("session_factory") or _default_session_factory


def _serialize_contact(c: Contact) -> dict[str, Any]:
    name = " ".join(p for p in [c.first_name, c.middle_name, c.last_name] if p)
    return {
        "id": c.id,
        "name": name,
        "first_name": c.first_name,
        "middle_name": c.middle_name,
        "last_name": c.last_name,
        "email": c.email,
        "phone": c.phone,
        "source": c.source,
        "created_at": serialize_value(c.created_at),
    }


def _serialize_event(e: ContactEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "event_type": e.event_type,
        "title": e.title,
        "description": e.description,
        "occurred_at": serialize_value(e.occurred_at),
        "created_at": serialize_value(e.created_at),
    }


# --- Model-facing argument schemas. None contain contact_id / account_id:
#     that omission is the structural guarantee (asserted by a test). ---

class GetContactArgs(BaseModel):
    """No arguments — operates on the contact this conversation is about."""


class ListContactEventsArgs(BaseModel):
    event_type: str | None = Field(
        default=None,
        description="Optional filter, e.g. 'call', 'email', 'meeting', 'note'.",
    )
    limit: int = Field(
        default=50, ge=1, le=200,
        description="Maximum number of events to return (most recent first).",
    )


class AddContactEventArgs(BaseModel):
    event_type: str = Field(
        description="Kind of interaction, e.g. 'call', 'email', 'meeting', 'note'."
    )
    title: str = Field(description="Short summary of what happened.")
    description: str | None = Field(
        default=None, description="Optional longer details."
    )


def _missing_contact_error() -> dict[str, str]:
    # Defensive only: prepare_agent_context fails closed before tools are even
    # built when a contact-scoped agent has no bound contact. This guards the
    # impossible path rather than silently running unscoped.
    return {"error": "No contact is bound to this conversation."}


async def create_contact_read_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Any = None,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    contact_id = kwargs.get("contact_id")
    # Closed over like contact_id: tools run AFTER the request session is
    # closed, on their own session, so there is no ambient context to read.
    #
    # Required, not optional. A tool built without a scope would query the
    # app database with no tenant filter at all, so failing to construct is
    # far better than running.
    org_scope = kwargs.get("org_scope")
    if org_scope is None:
        raise ValueError(
            "org_scope is required to build CRM tools — without it the tool "
            "would read across tenants."
        )
    session_factory = _resolve_session_factory(kwargs)

    async def get_contact() -> dict[str, Any]:
        if contact_id is None:
            return _missing_contact_error()
        s = session_factory()
        try:
            c = (
                s.query(Contact)
                .filter(Contact.id == contact_id, tenant_predicate(Contact, org_scope))
                .first()
            )
            return _serialize_contact(c) if c else {"error": "Contact not found."}
        finally:
            s.close()

    return StructuredTool(
        func=get_contact,
        coroutine=get_contact,
        name="get_contact",
        description="Get the details of the contact this conversation is about.",
        args_schema=GetContactArgs,
    )


async def create_contact_events_read_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Any = None,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    contact_id = kwargs.get("contact_id")
    # Closed over like contact_id: tools run AFTER the request session is
    # closed, on their own session, so there is no ambient context to read.
    #
    # Required, not optional. A tool built without a scope would query the
    # app database with no tenant filter at all, so failing to construct is
    # far better than running.
    org_scope = kwargs.get("org_scope")
    if org_scope is None:
        raise ValueError(
            "org_scope is required to build CRM tools — without it the tool "
            "would read across tenants."
        )
    session_factory = _resolve_session_factory(kwargs)

    async def list_contact_events(
        event_type: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        if contact_id is None:
            return _missing_contact_error()
        limit = max(1, min(int(limit), 200))
        s = session_factory()
        try:
            q = s.query(ContactEvent).filter(
                ContactEvent.contact_id == contact_id,
                tenant_predicate(ContactEvent, org_scope),
            )
            if event_type:
                q = q.filter(ContactEvent.event_type == event_type)
            rows = (
                q.order_by(ContactEvent.occurred_at.desc()).limit(limit).all()
            )
            return {"events": [_serialize_event(e) for e in rows]}
        finally:
            s.close()

    return StructuredTool(
        func=list_contact_events,
        coroutine=list_contact_events,
        name="list_contact_events",
        description="List logged activity events for this contact, most recent first.",
        args_schema=ListContactEventsArgs,
    )


async def create_contact_event_write_tool(
    tool_config: dict[str, Any],
    account_id: int,
    db: Any = None,
    auth_token: str | None = None,
    **kwargs,
) -> StructuredTool:
    contact_id = kwargs.get("contact_id")
    # Closed over like contact_id: tools run AFTER the request session is
    # closed, on their own session, so there is no ambient context to read.
    #
    # Required, not optional. A tool built without a scope would query the
    # app database with no tenant filter at all, so failing to construct is
    # far better than running.
    org_scope = kwargs.get("org_scope")
    if org_scope is None:
        raise ValueError(
            "org_scope is required to build CRM tools — without it the tool "
            "would read across tenants."
        )
    session_factory = _resolve_session_factory(kwargs)

    async def add_contact_event(
        event_type: str, title: str, description: str | None = None
    ) -> dict[str, Any]:
        if contact_id is None:
            return _missing_contact_error()
        s = session_factory()
        try:
            event = ContactEvent(
                contact_id=contact_id,      # forced — not a model argument
                org_id=org_scope.org_id,    # forced — the tenant
                account_id=account_id,      # forced — attribution
                event_type=event_type,
                title=title,
                description=description,
            )
            s.add(event)
            s.commit()
            s.refresh(event)
            return {"success": True, "event": _serialize_event(event)}
        except Exception as e:  # noqa: BLE001 - surface a tool-friendly error
            s.rollback()
            return {"error": str(e)}
        finally:
            s.close()

    return StructuredTool(
        func=add_contact_event,
        coroutine=add_contact_event,
        name="add_contact_event",
        description="Log a new activity event (call, email, meeting, note) for this contact.",
        args_schema=AddContactEventArgs,
    )
