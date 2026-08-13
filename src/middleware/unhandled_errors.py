"""Convert an unhandled exception into a response INSIDE the middleware stack.

Starlette's last-resort handler (`@app.exception_handler(Exception)`) runs in
ServerErrorMiddleware, which is the outermost layer — outside CORS. An
exception that reaches it produces a 500 whose headers no middleware ever
touched, so a browser sees a CORS failure rather than the error body, and the
UI cannot read the status at all.

That did not matter while every router ended in `except Exception: raise
handle_db_error(...)`, because the response was built inside the router. With
those removed, this middleware takes over the same job in one place.

It is added FIRST in main.py so that it ends up INNERMOST among the user
middlewares (Starlette's add_middleware prepends, so last-added is outermost).
Anything raised past the routing layer is converted here, and the resulting
response then travels back out through SlowAPI and CORS normally.

SQLAlchemy errors never get this far: they have registered handlers, which
Starlette's ExceptionMiddleware applies further in.
"""
import logging

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)


class UnhandledErrorMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            # exc_info so the traceback survives — the per-endpoint log tags
            # ("[UPDATE CONTACT]") that the old router tails carried are
            # replaced by the path plus a real stack.
            logger.error(
                "[UNHANDLED] %s %s | %s: %s",
                request.method, request.url.path, type(exc).__name__, exc,
                exc_info=exc,
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"detail": "An unexpected error occurred. Please try again."},
            )
