"""Per-request cluster log fields.

The four-tuple is ``user_id / agent_id / session_id / node_id``. Phase 1 leaves
``user_id`` empty. Fields are context-local, so a log line emitted on the
request task picks them up; a line with no active context does not invent them.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_active: ContextVar[bool] = ContextVar("hermes_cluster_log_active", default=False)
_user_id: ContextVar[str] = ContextVar("hermes_cluster_log_user", default="")
_agent_id: ContextVar[str] = ContextVar("hermes_cluster_log_agent", default="")
_session_id: ContextVar[str] = ContextVar("hermes_cluster_log_session", default="")
_node_id: ContextVar[str] = ContextVar("hermes_cluster_log_node", default="")


def set_log_context(
    *,
    user_id: str = "",
    agent_id: str = "",
    session_id: str = "",
    node_id: str = "",
) -> tuple[Token, Token, Token, Token, Token]:
    """Mark the current task as carrying the four-tuple. Empty strings stay empty."""
    return (
        _active.set(True),
        _user_id.set(user_id or ""),
        _agent_id.set(agent_id or ""),
        _session_id.set(session_id or ""),
        _node_id.set(node_id or ""),
    )


def reset_log_context(tokens: tuple[Token, Token, Token, Token, Token]) -> None:
    _node_id.reset(tokens[4])
    _session_id.reset(tokens[3])
    _agent_id.reset(tokens[2])
    _user_id.reset(tokens[1])
    _active.reset(tokens[0])


def cluster_log_tag() -> str:
    """`` [u= a= s= n=]`` when a request set the context, else ``\"\"``."""
    if not _active.get():
        return ""
    return f" [u={_user_id.get()} a={_agent_id.get()} s={_session_id.get()} n={_node_id.get()}]"
