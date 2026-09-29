"""
Agent Tools

Registry + factory for creating LangChain tools from an agent config.
Add a new tool type by writing a builder module and registering it below.
"""
from functools import partial

from .code_execution import create_code_execution_tool
from .contact_crm import (
    create_contact_event_write_tool,
    create_contact_events_read_tool,
    create_contact_read_tool,
)
from .db_read import create_db_read_tool
from .knowledge_delete import create_knowledge_delete_tool
from .knowledge_write import create_knowledge_write_tool
from .tariff_measures import (
    create_tariff_measure_search_tool,
    create_tariff_measure_update_tool,
)
from .time_series_forecast import create_time_series_forecast_tool
from .db_write import create_db_write_tool
from .exceptions import CredentialError
from .factory import create_tools_from_agent_config
from .registry import ToolRegistry
from .send_email_with_gmail import (
    create_send_email_with_google_oauth_tool,
    create_send_email_with_google_smtp_tool,
)
from .send_email_with_ses import create_send_email_with_ses_tool
from .send_html_email_with_ses import create_send_html_email_with_ses_tool
from .think import create_think_tool
from .vector_search import create_vector_search_tool

# ── Register all built-in tool types ────────────────────────────────────────
# Vector search is one builder; the reranking variant is the same tool with
# ``reranking=True``. Both type strings stay registered for backward compatibility.
ToolRegistry.register("vectorSearch", create_vector_search_tool)  # `rerank` in the config
# Legacy alias: the type reranking used to be. Kept so saved configs load.
ToolRegistry.register("vectorSearchWithReranking", partial(create_vector_search_tool, reranking=True))
ToolRegistry.register("dbTableRead", create_db_read_tool)
ToolRegistry.register("dbTableWrite", create_db_write_tool)
ToolRegistry.register("sendTxtEmailWithSes", create_send_email_with_ses_tool)
ToolRegistry.register("sendHtmlEmailWithSes", create_send_html_email_with_ses_tool)
ToolRegistry.register("sendTxtEmailWithGoogleOAuth", create_send_email_with_google_oauth_tool)
ToolRegistry.register("sendTxtEmailWithGoogleSmtp", create_send_email_with_google_smtp_tool)
ToolRegistry.register("contactRead", create_contact_read_tool)
ToolRegistry.register("contactEventsRead", create_contact_events_read_tool)
ToolRegistry.register("contactEventWrite", create_contact_event_write_tool)
# Internal reasoning — no external access. Registering a tool is what enables
# multi-step turns, so this is how a "toolless" agent gets a reasoning loop.
ToolRegistry.register("think", create_think_tool)
# Runner-backed (cmdlabs-runner does the work; see agent_runtime/runner_client.py)
# and the HITL knowledge writer. Built only when RUNNER_URL / a writable KB is
# configured; otherwise skipped with a log line like any other misconfigured tool.
ToolRegistry.register("timeSeriesForecast", create_time_series_forecast_tool)
ToolRegistry.register("codeExecution", create_code_execution_tool)
ToolRegistry.register("knowledgeWrite", create_knowledge_write_tool)
# HITL delete from a KB (routers/tool_approvals/knowledge_delete.py executes it).
ToolRegistry.register("knowledgeDelete", create_knowledge_delete_tool)
# Tariff measures: a read-only lookup, and HITL edits
# (routers/tool_approvals/tariff_measure_update.py executes them).
ToolRegistry.register("tariffMeasureSearch", create_tariff_measure_search_tool)
ToolRegistry.register("tariffMeasureUpdate", create_tariff_measure_update_tool)

__all__ = [
    "CredentialError",
    "ToolRegistry",
    "create_tools_from_agent_config",
]
