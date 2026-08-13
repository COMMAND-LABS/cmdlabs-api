"""Agent runtime — the streaming agent engine, formerly cmdlabs-agent-api.

This package is a vertical slice: the SSE agent stream, the contact-scoped
CRM chat, the PDF->FAQ generator, and the LangChain tool registry that backs
them. It was merged back into this service for iteration speed; it is kept
extractable so it can become its own service again if streaming load ever
needs independent scaling.

Two conventions keep the extraction seam clean — follow both:

1. One-way dependency. This package imports freely from the shared kernel
   (``src.db``, ``src.services``, ``src.config``, ``src.core``, ``src.deps``,
   ``src.utils``, ``src.rate_limit``), but nothing outside this package may
   import from ``src.agent_runtime`` except ``src.main`` mounting
   ``src.agent_runtime.router``.

2. Event-loop and pool hygiene. Streams never hold a DB connection while
   streaming (setup closes the request session before the first token; all
   later writes use short-lived sessions), and CPU-bound work (PyMuPDF) is
   dispatched via ``run_in_threadpool``, never run on the event loop. Both
   invariants are pinned by tests/agent_runtime/test_event_loop_hygiene.py.
"""
