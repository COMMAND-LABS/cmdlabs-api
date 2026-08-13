"""The error contract lives in main.py now, so it is asserted here.

Routers used to end in a `try/except HTTPException: raise / except Exception:
raise handle_db_error(...)` tail — the same six lines repeated ~150 times. The
tail is gone and the app-level handlers do the job, which means the guarantees
it carried per endpoint are now guarantees of ONE piece of wiring. If that
wiring regresses, every endpoint regresses at once, so it gets tests of its own.

Three things the old tails did that must still hold:

  1. An unexpected exception becomes a safe 500 rather than a stack trace, AND
     the response still carries CORS headers. This is the one that is easy to
     lose: Starlette's own last-resort handler runs OUTSIDE the CORS
     middleware, so a 500 built there reaches a browser as a CORS failure with
     no readable status. UnhandledErrorMiddleware exists to keep the response
     inside the stack.
  2. A deliberate HTTPException (a 404 guard, a 403) is NOT remapped to 500.
     chatSessions/router.py used to say this in a comment above its
     pass-through handler; it is an assertion now.
  3. An IntegrityError maps to a status that describes the constraint —
     including the foreign-key -> 400 branch, which only handle_db_error had
     until the tails were removed.
"""
import pytest
from fastapi import APIRouter, HTTPException, status
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import IntegrityError

from src.main import app, integrity_error_handler

ALLOWED_ORIGIN = "https://cmdlabs.io"


@pytest.fixture()
def probe_routes():
    """Mount probe endpoints on the real app, then remove them.

    The real app rather than a stand-in, because what is under test IS the
    wiring: middleware order and handler registration.
    """
    router = APIRouter()

    @router.get("/__probe/boom")
    async def boom():
        raise ValueError("kaboom")

    @router.get("/__probe/not-found")
    async def not_found():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nope")

    before = len(app.router.routes)
    app.include_router(router)
    yield
    del app.router.routes[before:]


@pytest.fixture()
async def probe_client(probe_routes) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test",
                           headers={"Origin": ALLOWED_ORIGIN}) as ac:
        yield ac


async def test_unexpected_exception_becomes_a_safe_500(probe_client: AsyncClient):
    r = await probe_client.get("/__probe/boom")

    assert r.status_code == 500
    assert r.json() == {"detail": "An unexpected error occurred. Please try again."}
    # The exception's own message must never reach the client.
    assert "kaboom" not in r.text
    assert "ValueError" not in r.text


async def test_a_500_still_carries_cors_headers(probe_client: AsyncClient):
    """Without this the UI sees a CORS error and cannot read the status at all.

    Regression guard for the middleware ORDER: UnhandledErrorMiddleware has to
    stay inner to DynamicCORSMiddleware, which means it has to stay added
    BEFORE it in main.py.
    """
    r = await probe_client.get("/__probe/boom")

    assert r.status_code == 500
    assert r.headers.get("access-control-allow-origin") == ALLOWED_ORIGIN


async def test_a_deliberate_http_error_is_not_remapped(probe_client: AsyncClient):
    r = await probe_client.get("/__probe/not-found")

    assert r.status_code == 404
    assert r.json()["detail"] == "Nope"


def _integrity_error(message: str) -> IntegrityError:
    return IntegrityError("INSERT ...", {}, Exception(message))


@pytest.mark.parametrize("message, expected_status", [
    ("duplicate key value violates unique constraint", 409),
    ("insert or update violates foreign key constraint", 400),
    ("some other integrity problem", 409),
])
async def test_integrity_errors_map_to_a_describing_status(message, expected_status):
    """The foreign-key branch came from handle_db_error when the tails went.

    A request naming a parent row that does not exist is the caller's mistake
    (400), not a conflict with data that does (409).
    """
    class _Req:
        url = type("U", (), {"path": "/api/test"})()

    response = await integrity_error_handler(_Req(), _integrity_error(message))

    assert response.status_code == expected_status
