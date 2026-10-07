"""Guard: raw exception text must never reach a client.

The text of an SDK / driver / crypto exception can carry secrets — AWS
signature errors echo the access key id, SQLAlchemy URL errors echo the whole
connection string, LLM SDK validation errors echo the API key they were given.
So inside an `except ... as exc:` block, `exc` may only be turned into text for
the LOG. Anything a client, the browser (SSE) or the model (tool results) sees
must use a fixed message or src.utils.errors.public_reason(exc).

This test walks every module under src/ and fails if a caught exception's text
(`str(exc)`, `f"...{exc}"`, `repr(exc)`, `exc.args`) flows into one of the
client-visible sinks below. Exceptions whose messages WE write, and which are
therefore safe to show, are allowlisted by type.
"""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# Calls whose arguments reach a client (HTTP body, SSE frame) or the model.
SINK_CALLS = {
    "HTTPException",
    "sse_error",
    "AgentSetupError",
    "CredentialError",
    "SesSendError",
    "LlmInitError",
    "CredentialDecryptError",
}
# Dict keys whose values are returned to a client or to the model.
SINK_KEYS = {"error", "detail", "reason", "message", "error_message"}

# Exception types raised with messages written by this codebase — safe to show.
SAFE_EXCEPTION_TYPES = {
    "HTTPException",
    "AccountGcsCredentialMissing",
    "SkillMarkdownError",
    "DatasetError",          # routers/datasets: CSV checks, fixed messages
    "SesSendError",          # built from public_reason (services/email_dispatch)
    "MissingVariablesError",
    "RunnerError",           # runner_client: detail is the runner's own message
    "JSONDecodeError",       # parsing the caller's own request body
    "AgentSetupError",
    "LlmSetupError",
    "MissingCredentialError",
    "CredentialDecryptError",
    "LlmInitError",
    "CredentialError",
    "KnowledgeTargetError",  # tools/knowledge_delete: target lookup, fixed messages
}


def _exc_types(handler: ast.ExceptHandler) -> set:
    node = handler.type
    elts = node.elts if isinstance(node, ast.Tuple) else [node]
    names = set()
    for e in elts:
        if isinstance(e, ast.Name):
            names.add(e.id)
        elif isinstance(e, ast.Attribute):
            names.add(e.attr)
    return names


def _mentions_text_of(node: ast.AST, name: str) -> bool:
    """True if `node` turns the exception bound to `name` into text."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.FormattedValue) and isinstance(sub.value, ast.Name) \
                and sub.value.id == name:
            return True
        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name) \
                and sub.func.id in ("str", "repr") and sub.args \
                and isinstance(sub.args[0], ast.Name) and sub.args[0].id == name:
            return True
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                and sub.value.id == name and sub.attr == "args":
            return True
    return False


def _call_name(call: ast.Call) -> str | None:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _leaks(handler: ast.ExceptHandler) -> list:
    name = handler.name
    found = []
    for node in ast.walk(ast.Module(body=handler.body, type_ignores=[])):
        if isinstance(node, ast.Call) and _call_name(node) in SINK_CALLS:
            args = list(node.args) + [k.value for k in node.keywords]
            if any(_mentions_text_of(a, name) for a in args):
                found.append((node.lineno, _call_name(node)))
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value in SINK_KEYS \
                        and _mentions_text_of(v, name):
                    found.append((node.lineno, f"{{'{k.value}': ...}}"))
        elif isinstance(node, ast.keyword) and node.arg in SINK_KEYS \
                and _mentions_text_of(node.value, name):
            found.append((node.value.lineno, f"{node.arg}=..."))
    return found


def test_no_exception_text_reaches_a_client():
    problems = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for handler in ast.walk(tree):
            if not isinstance(handler, ast.ExceptHandler) or not handler.name:
                continue
            if _exc_types(handler) & SAFE_EXCEPTION_TYPES:
                continue
            for lineno, sink in _leaks(handler):
                problems.append(f"{path.relative_to(SRC.parent)}:{lineno} -> {sink}")
    assert not problems, (
        "Exception text flows to a client. Log the exception and send a fixed "
        "message or public_reason(exc) instead:\n  " + "\n  ".join(problems)
    )
