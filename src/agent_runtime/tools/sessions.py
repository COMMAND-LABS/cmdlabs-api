"""Short-lived DB session factory for agent tools.

Agent tools run AFTER the request session is closed (prepare_agent_context
closes it before the agent loop starts), so any tool that touches the app
database opens its own short-lived session, commits, and closes — the same
pattern as persist_ai_message.

The factory is injectable via the ``session_factory`` builder kwarg so tool
behavior can be unit-tested against a disposable database without
monkeypatching. The default imports SessionLocal lazily so importing a tool
module never triggers DB engine setup.
"""

from collections.abc import Callable
from typing import Any


def default_session_factory():
    from src.db.database import SessionLocal
    return SessionLocal()


def resolve_session_factory(kwargs: dict[str, Any]) -> Callable[[], Any]:
    return kwargs.get("session_factory") or default_session_factory
