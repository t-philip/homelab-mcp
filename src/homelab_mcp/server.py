"""Assemble an MCP server from configuration.

The MCP SDK (``mcp`` >= 2) is used only in this module. Everything else works
with plain adapters and :class:`ToolSpec`, so a future SDK change stays
contained here — including the field-name and exception differences between SDK
major versions.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .config import PUBLIC_BIND_ADDRESSES, Config
from .errors import HomelabMcpError
from .registry import LoadedAdapters, build_adapters

if TYPE_CHECKING:
    from starlette.applications import Starlette

logger = logging.getLogger("homelab_mcp.server")

_INSTRUCTIONS = (
    "Read-only access to homelab infrastructure. Every tool only reads state; "
    "none of them change anything. If a change is needed, report the command for "
    "a human to run."
)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)


def _as_tool_error(
    name: str, handler: Callable[..., Awaitable[Any]]
) -> Callable[..., Awaitable[Any]]:
    """Translate our own errors into the SDK's ``ToolError``.

    An expected failure — a host outside the allowlist, a bad unit name, an
    unreachable backend — should reach the model as a tool error, not crash the
    server. Anything that is *not* a :class:`HomelabMcpError` is a real bug and
    is left to propagate as a crash.
    """

    @functools.wraps(handler)
    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except HomelabMcpError as exc:
            logger.warning("tool %s failed: %s", name, exc)
            raise ToolError(str(exc)) from exc

    return wrapper


def build_server(
    config: Config, *, environ: Mapping[str, str] | None = None
) -> tuple[MCPServer, LoadedAdapters]:
    """Build a ready-to-run :class:`MCPServer` and the adapters backing it."""
    loaded = build_adapters(config, environ=environ)
    mcp: MCPServer = MCPServer(name="homelab-mcp", instructions=_INSTRUCTIONS)
    for spec in loaded.tools:
        mcp.add_tool(
            _as_tool_error(spec.name, spec.handler),
            name=spec.name,
            description=spec.description,
            annotations=_READ_ONLY,
        )
    return mcp, loaded


def _transport_security(config: Config) -> TransportSecuritySettings:
    """Which Host / Origin headers the HTTP transport will accept.

    The SDK enables DNS-rebinding protection by default; with an empty allow
    list it rejects *every* request (HTTP 421). So an allow list is always
    supplied — from ``[server].allowed_hosts`` / ``allowed_origins`` if set,
    otherwise a localhost-plus-configured-host default.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=config.server.effective_allowed_hosts(),
        allowed_origins=list(config.server.allowed_origins),
    )


def build_http_app(
    config: Config, *, environ: Mapping[str, str] | None = None
) -> tuple[Starlette, LoadedAdapters]:
    """Build the Starlette ASGI app for the ``streamable-http`` transport.

    Useful for hosting behind a real ASGI server, and for tests. Bind the
    resulting app to a LAN or VPN address only. The caller owns the returned
    :class:`LoadedAdapters` and must ``await`` its ``aclose()`` on shutdown.
    """
    _warn_if_public(config)
    mcp, loaded = build_server(config, environ=environ)
    app = mcp.streamable_http_app(
        streamable_http_path=config.server.http_path,
        host=config.server.host,
        transport_security=_transport_security(config),
    )
    return app, loaded


def _warn_if_public(config: Config) -> None:
    server = config.server
    if server.transport != "stdio" and server.host in PUBLIC_BIND_ADDRESSES:
        logger.warning(
            "binding %s transport to %s:%s — this is reachable beyond localhost; "
            "keep it on a LAN or VPN only, never a public interface",
            server.transport,
            server.host,
            server.port,
        )


async def run_server(config: Config, *, environ: Mapping[str, str] | None = None) -> None:
    """Build and serve on the configured transport, cleaning up adapters on exit."""
    mcp, loaded = build_server(config, environ=environ)
    server = config.server
    _warn_if_public(config)
    logger.info(
        "starting homelab-mcp on %s%s",
        server.transport,
        f" ({server.host}:{server.port}{server.http_path})" if server.transport != "stdio" else "",
    )
    try:
        if server.transport == "stdio":
            await mcp.run_stdio_async()
        elif server.transport == "streamable-http":
            await mcp.run_streamable_http_async(
                host=server.host,
                port=server.port,
                streamable_http_path=server.http_path,
                transport_security=_transport_security(config),
            )
        elif server.transport == "sse":
            await mcp.run_sse_async(
                host=server.host,
                port=server.port,
                transport_security=_transport_security(config),
            )
        else:  # pragma: no cover - config validation prevents this
            raise ValueError(f"unsupported transport {server.transport!r}")
    finally:
        logger.info("homelab-mcp shutting down")
        await loaded.aclose()
