from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.config import load_config_text
from homelab_mcp.errors import ConfigError
from homelab_mcp.registry import build_adapters
from homelab_mcp.server import build_http_app, build_server

_CONFIG = """
[server]
transport = "stdio"

[adapters.netbox]
enabled = true
base_url = "https://netbox.example.invalid"
token_env = "NETBOX_TOKEN"

[adapters.systemd]
enabled = true
hosts = ["node-a"]
"""

_ENV = {"NETBOX_TOKEN": "value"}


def test_build_adapters_collects_tools() -> None:
    cfg = load_config_text(_CONFIG)
    loaded = build_adapters(cfg, environ=_ENV)
    names = {spec.name for spec in loaded.tools}
    assert "netbox_find_device" in names
    assert "systemd_failed_units" in names
    assert len(loaded.adapters) == 2


def test_build_adapters_missing_secret_fails_closed() -> None:
    cfg = load_config_text(_CONFIG)
    with pytest.raises(ConfigError, match="NETBOX_TOKEN"):
        build_adapters(cfg, environ={})


def test_unknown_enabled_adapter_rejected() -> None:
    cfg = load_config_text("[adapters.telepathy]\nenabled = true\n")
    with pytest.raises(ConfigError, match="no such adapter"):
        build_adapters(cfg, environ={})


def test_missing_required_option_fails_at_startup() -> None:
    # grafana enabled but no base_url -> must not start, and not deferred to
    # first tool call).
    cfg = load_config_text('[adapters.grafana]\nenabled = true\ntoken_env = "GRAF"\n')
    with pytest.raises(ConfigError, match=r"\[adapters.grafana\] requires 'base_url'"):
        build_adapters(cfg, environ={"GRAF": "x"})


def test_influxdb_missing_org_fails_at_startup() -> None:
    cfg = load_config_text(
        "[adapters.influxdb]\nenabled = true\n"
        'base_url = "https://i.example.invalid"\ntoken_env = "IT"\n'
    )
    with pytest.raises(ConfigError, match="requires 'org'"):
        build_adapters(cfg, environ={"IT": "x"})


async def test_build_server_registers_read_only_tools() -> None:
    cfg = load_config_text(_CONFIG)
    mcp, loaded = build_server(cfg, environ=_ENV)
    try:
        tools = await mcp.list_tools()
    finally:
        await loaded.aclose()

    assert tools, "expected tools to be registered"
    by_name = {t.name: t for t in tools}
    assert "proxmox_list_nodes" not in by_name  # proxmox not enabled
    assert "netbox_list_devices" in by_name
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False


async def test_build_server_tool_schema_has_documented_params() -> None:
    cfg = load_config_text(_CONFIG)
    mcp, loaded = build_server(cfg, environ=_ENV)
    try:
        tools = {t.name: t for t in await mcp.list_tools()}
    finally:
        await loaded.aclose()

    schema = tools["systemd_recent_log"].input_schema
    assert set(schema["required"]) >= {"host", "unit"}
    assert "lines" in schema["properties"]


async def test_call_tool_translates_adapter_errors_to_tool_error() -> None:
    """An expected adapter failure becomes the SDK's deliberate ``ToolError``.

    Not ``UnexpectedToolError`` — that is reserved for real crashes — so the MCP
    client sees it as a tool error result rather than a server fault.
    """
    from mcp.server.mcpserver.exceptions import ToolError, UnexpectedToolError

    cfg = load_config_text(_CONFIG)
    mcp, loaded = build_server(cfg, environ=_ENV)
    try:
        with pytest.raises(ToolError) as exc:
            await mcp.call_tool(
                "systemd_service_status",
                {"host": "not-in-allowlist", "unit": "x.service"},
            )
    finally:
        await loaded.aclose()
    assert not isinstance(exc.value, UnexpectedToolError)
    assert "allowlist" in str(exc.value)


@respx.mock
async def test_call_tool_happy_path_returns_content() -> None:
    """A successful tool call is driven end to end through the server."""
    from mcp.types import CallToolResult

    respx.get("https://netbox.example.invalid/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json={"count": 0, "results": []})
    )
    cfg = load_config_text(_CONFIG)
    mcp, loaded = build_server(cfg, environ=_ENV)
    try:
        result = await mcp.call_tool("netbox_list_devices", {"limit": 5})
    finally:
        await loaded.aclose()
    assert isinstance(result, CallToolResult)
    assert not result.is_error
    assert result.structured_content == {"result": []}


async def test_build_http_app_mounts_the_configured_path() -> None:
    cfg = load_config_text(
        """
[server]
transport = "streamable-http"
host = "127.0.0.1"
port = 9123
http_path = "/homelab"

[adapters.systemd]
enabled = true
hosts = ["node-a"]
"""
    )
    app, loaded = build_http_app(cfg)
    try:
        from starlette.applications import Starlette

        assert isinstance(app, Starlette)
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/homelab" in paths, paths  # config's http_path was honoured
    finally:
        await loaded.aclose()


@respx.mock
async def test_full_protocol_round_trip_via_mcp_client() -> None:
    """initialize -> list_tools -> call_tool, through the real MCP protocol.

    The other tests call MCPServer methods directly; this goes through the SDK
    client so the initialize handshake and JSON-RPC framing are exercised too.
    """
    import mcp

    respx.get("https://netbox.example.invalid/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json={"count": 0, "results": []})
    )
    srv, loaded = build_server(load_config_text(_CONFIG), environ=_ENV)
    try:
        async with mcp.Client(srv) as client:
            listed = await client.list_tools()
            names = {t.name for t in listed.tools}
            assert {"netbox_list_devices", "systemd_failed_units"} <= names
            for tool in listed.tools:
                assert tool.annotations and tool.annotations.read_only_hint

            ok = await client.call_tool("netbox_list_devices", {"limit": 5})
            assert not ok.is_error
            assert ok.structured_content == {"result": []}

            bad = await client.call_tool(
                "systemd_service_status", {"host": "not-allowed", "unit": "x.service"}
            )
            assert bad.is_error
            texts = " ".join(getattr(c, "text", "") for c in bad.content)
            assert "allowlist" in texts
    finally:
        await loaded.aclose()


async def test_http_transport_serves_the_tool_list_over_asgi() -> None:
    """Drive build_http_app()'s ASGI app over HTTP: initialize then tools/list."""
    import json as _json

    cfg = load_config_text(_CONFIG)
    app, loaded = build_http_app(cfg, environ=_ENV)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    init = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"},
        },
    }
    try:
        from asgi_lifespan import LifespanManager

        transport = httpx.ASGITransport(app=app)
        async with (
            LifespanManager(app),
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http,
        ):
            r1 = await http.post("/mcp", headers=headers, json=init)
            assert r1.status_code == 200
            session = r1.headers.get("mcp-session-id")
            assert session, r1.headers

            r2 = await http.post(
                "/mcp",
                headers={**headers, "mcp-session-id": session},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            )
            assert r2.status_code == 200
            body = r2.text
            if body.startswith("event:") or "data:" in body:
                body = body.split("data:", 1)[1].strip()
            payload = _json.loads(body)
            tool_names = {t["name"] for t in payload["result"]["tools"]}
            assert "netbox_list_devices" in tool_names
    finally:
        await loaded.aclose()
