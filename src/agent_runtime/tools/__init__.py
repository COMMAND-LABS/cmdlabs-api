"""
Agent Tools

Registry + factory for creating LangChain tools from an agent config.
Add a new tool type by writing a builder module and registering it below.
"""
from functools import partial

from .contact_crm import (
    create_contact_event_write_tool,
    create_contact_events_read_tool,
    create_contact_read_tool,
)
from .db_read import create_db_read_tool
from .db_write import create_db_write_tool
from .exceptions import CredentialError
from .factory import create_tools_from_agent_config
from .registry import ToolRegistry
from .send_email_with_ses import create_send_email_with_ses_tool
from .send_html_email_with_ses import create_send_html_email_with_ses_tool
from .vector_search import create_vector_search_tool

# ── Register all built-in tool types ────────────────────────────────────────
# Vector search is one builder; the reranking variant is the same tool with
# ``reranking=True``. Both type strings stay registered for backward compatibility.
ToolRegistry.register("vectorSearch", partial(create_vector_search_tool, reranking=False))
ToolRegistry.register("vectorSearchWithReranking", partial(create_vector_search_tool, reranking=True))
ToolRegistry.register("dbTableRead", create_db_read_tool)
ToolRegistry.register("dbTableWrite", create_db_write_tool)
ToolRegistry.register("sendTxtEmailWithSes", create_send_email_with_ses_tool)
ToolRegistry.register("sendHtmlEmailWithSes", create_send_html_email_with_ses_tool)
ToolRegistry.register("contactRead", create_contact_read_tool)
ToolRegistry.register("contactEventsRead", create_contact_events_read_tool)
ToolRegistry.register("contactEventWrite", create_contact_event_write_tool)

__all__ = [
    "CredentialError",
    "ToolRegistry",
    "create_tools_from_agent_config",
]
