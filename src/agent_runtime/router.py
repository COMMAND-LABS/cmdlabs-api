"""The agent runtime's public surface.

``src.main`` mounts these three routers (via its gated ``_ROUTERS`` registry);
nothing else imports from this package. Paths are unchanged from the
standalone agent-api service so the UI cutover is only a base-URL change:

  POST /api/agents/{agent_id}/stream         — SSE agent completion
  POST /api/contact-chat/{session_id}/stream — SSE contact-scoped CRM chat
  POST /api/pdf-to-faq/generate              — one-shot PDF -> FAQ JSON

They are exported separately (not under one APIRouter) so each prefix gets
its own module gate from src/config/modules_registry.py: agents /
contacts / knowledge_bases respectively.

The agents CRUD routes stay in ``src.routers.agents``; only the stream lives
here (the standalone service's duplicate ``GET /api/agents/{agent_id}`` was
dropped in favor of the CRUD router's copy).
"""
from src.agent_runtime.contact_chat import router as contact_chat_router
from src.agent_runtime.pdf_to_faq import router as pdf_to_faq_router
from src.agent_runtime.stream import router as agent_stream_router

__all__ = ["agent_stream_router", "contact_chat_router", "pdf_to_faq_router"]
