"""Exception hierarchy for homelab-mcp."""

from __future__ import annotations


class HomelabMcpError(Exception):
    """Base class for every error raised by this package."""


class ConfigError(HomelabMcpError):
    """The configuration file is missing, malformed, or incomplete.

    Raised at startup. An enabled adapter whose required credentials or
    endpoints are absent is a ``ConfigError`` — the server fails closed rather
    than starting in a half-configured state.
    """


class AdapterError(HomelabMcpError):
    """An adapter could not be constructed or used."""


class UpstreamError(AdapterError):
    """An enabled adapter's backing service returned an error or was unreachable.

    This is surfaced to the MCP client as a tool error, not a server crash —
    one unhealthy backend must not take the whole server down.
    """
