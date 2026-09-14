"""The read-only guarantee, expressed as a rule about tool names.

Every tool registered by every adapter must pass :func:`tool_name_violation`.
The check runs three times over: defensively inside the registry when adapters
are loaded, as a unit test over the real adapter set, and as a standalone lint
(``scripts/check_no_write_tools.py``) wired into CI.

The rule is deliberately about the *leading verb* of the tool name. A homelab
query tool is named for what it returns (``list_nodes``, ``guest_status``,
``recent_log``); a tool whose name starts with a state-changing verb is assumed
to change state and is rejected, even if its implementation happens not to.
"""

from __future__ import annotations

#: Verbs that imply a state change. A tool name whose first ``_``-delimited
#: token is one of these is rejected.
FORBIDDEN_TOOL_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "apply",
        "clone",
        "copy",
        "create",
        "delete",
        "deploy",
        "destroy",
        "disable",
        "drain",
        "edit",
        "enable",
        "exec",
        "execute",
        "install",
        "kill",
        "migrate",
        "modify",
        "move",
        "patch",
        "post",
        "power",
        "poweroff",
        "poweron",
        "provision",
        "purge",
        "push",
        "put",
        "reboot",
        "remove",
        "rename",
        "reset",
        "resize",
        "restart",
        "rollback",
        "rotate",
        "run",
        "set",
        "shutdown",
        "snapshot",
        "start",
        "stop",
        "terminate",
        "toggle",
        "uninstall",
        "update",
        "upgrade",
        "write",
    }
)


def tool_name_violation(name: str) -> str | None:
    """Return a reason string if *name* looks like a state-mutating tool.

    Returns ``None`` when the name is acceptable for a read-only tool.
    """
    if not name:
        return "tool name is empty"
    if not name.replace("_", "").isalnum() or not name.islower():
        return f"tool name {name!r} must be lower_snake_case alphanumerics"
    head = name.split("_", 1)[0]
    if head in FORBIDDEN_TOOL_VERBS:
        return f"tool name {name!r} starts with the state-changing verb {head!r}"
    return None
