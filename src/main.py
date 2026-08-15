import logging
import os

# Configure the root logger so application `logger.info(...)` calls actually
# emit. Without this, no root handler exists and Python's last-resort handler
# only shows WARNING+, so all INFO/DEBUG app logs were being silently dropped
# (uvicorn configures only its own loggers, not the root logger).
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from src.middleware.dynamic_cors import DynamicCORSMiddleware
from src.middleware.unhandled_errors import UnhandledErrorMiddleware
from src.rate_limit import limiter

logger = logging.getLogger(__name__)

from .routers import (
    healthcheck,
    auth,
    logins,
    waitlist,
    chatSessions,
    billing,
    credentials,
    vectorStores,
    agents,
    apiKeys,
    accounts,
    prompts,
    skills,
    similaritySearch,
    contacts,
)
from .routers import access as access_audit
from .routers import contact_lists
from .routers import companies
from .routers import files
from .routers import deals
from .routers import tool_approvals
from .routers import email_events
from .routers import email_templates
from .routers import email_campaigns
from .routers import emails
from .routers import tracking
from .routers import feedback
from .routers import admin
from .routers import organizations
from .routers import app_settings
from .routers import courses
from .routers import llm_chat
from .routers import memory_chat
from .agent_runtime.router import (
    agent_stream_router,
    contact_chat_router,
    pdf_to_faq_router,
)

app = FastAPI(
    docs_url="/api/docs",
    redoc_url=None,
    redirect_slashes=True,
)

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler for 422 validation errors to provide detailed error messages.
    This helps with debugging API calls from the frontend.
    """
    errors = exc.errors()
    logger.warning("[VALIDATION ERROR] %s %s: %s", request.method, request.url.path, errors)
    
    error_details = []
    for error in errors:
        location = " -> ".join(str(loc) for loc in error["loc"])
        error_details.append({
            "location": location,
            "message": error["msg"],
            "type": error["type"]
        })
    
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "Validation Error",
            "message": "The request could not be processed due to validation errors.",
            "details": error_details,
            "path": str(request.url.path)
        }
    )


@app.exception_handler(IntegrityError)
async def integrity_error_handler(request: Request, exc: IntegrityError):
    """Catch any SQLAlchemy IntegrityError that bubbles past a router.

    These three handlers ARE the error contract for every endpoint. Routers used
    to repeat a `try/except Exception: raise handle_db_error(...)` tail 150-odd
    times to produce exactly these responses; the tail is gone, and this is the
    one place that maps a database failure onto a status code.

    The per-endpoint log tag those tails carried ("[UPDATE CONTACT]") is
    replaced by request.url.path, which names the same endpoint without having
    to be kept in sync with the function it sits in.
    """
    logger.error("[INTEGRITY ERROR] Path: %s | %s: %s", request.url.path, type(exc).__name__, exc)
    orig = getattr(exc, "orig", None)
    msg = str(orig).lower() if orig else str(exc).lower()
    if "unique" in msg or "duplicate" in msg:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "A record with that value already exists."},
        )
    if "foreign key" in msg or "violates foreign" in msg:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "The request references a resource that does not exist."},
        )
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={"detail": "The request conflicts with existing data."},
    )


@app.exception_handler(SQLAlchemyError)
async def sqlalchemy_error_handler(request: Request, exc: SQLAlchemyError):
    """Catch any other SQLAlchemy error that bubbles past a router."""
    logger.error("[DB ERROR] Path: %s | %s: %s", request.url.path, type(exc).__name__, exc)
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"detail": "A database error occurred. Please try again."},
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Last-resort handler — never let raw exception details reach the client."""
    logger.error("[UNHANDLED] Path: %s | %s: %s", request.url.path, type(exc).__name__, exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "An unexpected error occurred. Please try again."},
    )


# Allowed origins for JWT/cookie authentication (internal UI)
jwt_allowed_origins = [
    "https://kalygo.io",
    "https://bolay.kalygo.io",
    "https://cmdlabs.io",
    "https://www.cmdlabs.io",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:3001",
    "http://127.0.0.1:3002",
    "http://localhost:3000",
    "https://kalygo-nextjs-service-830723611668.us-east1.run.app",
    "https://localhost:3000",
    "http://localhost:5000",  # Second FastAPI
]

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Added FIRST, so it sits INNERMOST (add_middleware prepends; last added runs
# first). An unhandled exception is turned into a 500 response here, which then
# travels back out through SlowAPI and CORS — so the browser gets the error
# instead of a CORS failure. See src/middleware/unhandled_errors.py.
app.add_middleware(UnhandledErrorMiddleware)

app.add_middleware(SlowAPIMiddleware)

# Dynamic CORS middleware (added last, so runs first to handle OPTIONS preflight)
# - API key requests: Allow all origins (for third-party integrations)
# - JWT/cookie requests: Restrict to jwt_allowed_origins (for internal UI)
app.add_middleware(
    DynamicCORSMiddleware,
    allowed_origins=jwt_allowed_origins,
    allow_credentials=True
)

# Router registration: (router, prefix, tags). Order is preserved from the
# original explicit calls. healthcheck mounts at the root with no tags;
# tracking is mounted under /t. Everything else lives under /api/...
_ROUTERS = [
    (healthcheck.router, "", None),
    (auth.router, "/api/auth", ["auth"]),
    (waitlist.router, "/api/waitlist", ["waitlist"]),
    (logins.router, "/api/logins", ["logins"]),
    (similaritySearch.router, "/api/similarity-search", ["Similarity Search"]),
    (chatSessions.router, "/api/chat-sessions", ["Chat Sessions"]),
    (billing.router, "/api/billing", ["Billing"]),
    (credentials.router, "/api/credentials", ["Credentials"]),
    (vectorStores.router, "/api/vector-stores", ["Vector Stores"]),
    (agents.router, "/api/agents", ["Agents"]),
    (apiKeys.router, "/api/api-keys", ["API Keys"]),
    (accounts.router, "/api/accounts", ["Accounts"]),
    (app_settings.router, "/api/app-settings", ["App Settings"]),
    (prompts.router, "/api/prompts", ["Prompts"]),
    (skills.router, "/api/skills", ["Skills"]),
    (access_audit.router, "/api/access", ["Access Audit"]),
    (contacts.router, "/api/contacts", ["Contacts"]),
    (contact_lists.router, "/api/contact-lists", ["Contact Lists"]),
    (companies.router, "/api/companies", ["Companies"]),
    (files.router, "/api/files", ["Files"]),
    (deals.router, "/api/deals", ["Deals"]),
    (tool_approvals.router, "/api/tool-approvals", ["Tool Approvals"]),
    (email_events.router, "/api/email-events", ["Email Events"]),
    (email_templates.router, "/api/email-templates", ["Email Templates"]),
    (email_campaigns.router, "/api/email-campaigns", ["Email Campaigns"]),
    (emails.router, "/api/emails", ["Emails"]),
    (tracking.router, "/t", ["Tracking"]),
    (feedback.router, "/api/feedback", ["Feedback"]),
    (courses.router, "/api/courses", ["Courses"]),
    # Direct LLM completions (no agent). Premium-gated via the llm_chat module.
    (llm_chat.router, "/api/llm-chat", ["LLM Chat"]),
    # The context-window teaching demo — llm-chat plus a server-held transcript.
    (memory_chat.router, "/api/memory-chat", ["Memory Chat"]),
    (organizations.router, "/api/organizations", ["Organizations"]),
    (admin.router, "/api/admin", ["Platform Admin"]),
    # Agent runtime (formerly cmdlabs-agent-api) — SSE streaming endpoints.
    # Same paths the standalone service served, so cutover is a base-URL swap.
    (agent_stream_router, "/api/agents", ["Agent Runtime"]),
    (contact_chat_router, "/api/contact-chat", ["Contact Chat"]),
    (pdf_to_faq_router, "/api/pdf-to-faq", ["PDF to FAQ"]),
]

# Module gating is derived from the registry rather than hand-written on each
# router. A router either maps to a module (and is gated) or is absent from the
# registry (and is visibly always-allowed) — there is no third state where
# somebody simply forgot to add the dependency.
#
# tests/test_module_enforcement.py asserts every prefix here is classified, so
# adding a router without deciding turns into a failing test rather than an
# ungated endpoint.
from fastapi import Depends

from src.config.modules_registry import module_for_path
from src.deps import require_module

for _router, _prefix, _tags in _ROUTERS:
    _module = module_for_path(_prefix) if _prefix else None
    _deps = [Depends(require_module(_module.key))] if _module else None
    app.include_router(_router, prefix=_prefix, tags=_tags, dependencies=_deps)
