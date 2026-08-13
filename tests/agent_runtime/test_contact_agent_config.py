"""No-DB tests for the code-defined contact agent + fail-closed helper."""

from src.agent_runtime.contact_agent_config import (
    CONTACT_AGENT_CONFIG,
    CONTACT_SCOPED_TOOL_TYPES,
    contact_session_required,
)
from src.agent_runtime.tools import ToolRegistry


def test_config_shape_is_v4():
    assert CONTACT_AGENT_CONFIG["schema"] == "agent_config"
    assert CONTACT_AGENT_CONFIG["version"] == 4
    data = CONTACT_AGENT_CONFIG["data"]
    assert data["systemPrompt"]
    assert data["model"]["provider"] and data["model"]["model"]
    # v4 `data` forbids extra props (e.g. a name field).
    assert set(data.keys()) <= {"systemPrompt", "model", "elevenlabsVoiceId", "tools"}


def test_config_tools_are_all_contact_scoped_and_registered():
    types = [t["type"] for t in CONTACT_AGENT_CONFIG["data"]["tools"]]
    assert types  # not empty
    for t in types:
        assert t in CONTACT_SCOPED_TOOL_TYPES
        # Every declared tool type must be registered in the factory.
        assert ToolRegistry.get_builder(t) is not None


def test_contact_session_required_true_for_contact_agent():
    assert contact_session_required(CONTACT_AGENT_CONFIG["data"]) is True


def test_contact_session_required_false_for_plain_agent():
    assert contact_session_required({"tools": []}) is False
    assert contact_session_required({}) is False
    assert contact_session_required({"tools": [{"type": "vectorSearch"}]}) is False


def test_contact_session_required_true_if_any_scoped_tool_present():
    cfg = {"tools": [{"type": "vectorSearch"}, {"type": "contactEventWrite"}]}
    assert contact_session_required(cfg) is True
