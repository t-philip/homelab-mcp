"""homelab-mcp — a read-only MCP server for querying homelab infrastructure.

Every tool this server exposes is read-only by construction. No adapter may
register a tool that changes state; this is enforced by
:mod:`homelab_mcp.naming` and the ``check_no_write_tools`` lint.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
