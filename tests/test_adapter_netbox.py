from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters.netbox import NetboxAdapter
from homelab_mcp.errors import HomelabMcpError

from .conftest import load_fixture_json, make_options

BASE = "https://netbox.example.invalid"


def _adapter() -> NetboxAdapter:
    return NetboxAdapter(
        make_options(
            raw={"base_url": BASE, "token_env": "NETBOX_TOKEN", "verify_tls": True},
            secrets={"NETBOX_TOKEN": "token-value"},
        )
    )


@respx.mock
async def test_find_device_by_exact_name() -> None:
    route = respx.get(f"{BASE}/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json=load_fixture_json("netbox", "device_one.json"))
    )
    adapter = _adapter()
    try:
        rows = await adapter.netbox_find_device(name="test-server-01")
    finally:
        await adapter.aclose()

    assert route.called
    assert len(rows) == 1
    dev = rows[0]
    assert dev["name"] == "test-server-01"
    assert dev["status"] == "Active"
    assert dev["role"] == "Server"
    assert dev["device_type"] == "Example Compute Example Server R1"
    assert dev["primary_ip4"] == "192.0.2.12/24"
    assert dev["site"] == "Test Site"


@respx.mock
async def test_find_device_sends_auth_header() -> None:
    route = respx.get(f"{BASE}/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json=load_fixture_json("netbox", "device_one.json"))
    )
    adapter = _adapter()
    try:
        await adapter.netbox_find_device(name="test-server-01")
    finally:
        await adapter.aclose()
    assert route.calls.last.request.headers["Authorization"] == "Token token-value"


@respx.mock
async def test_list_devices_clamps_limit_and_passes_filters() -> None:
    route = respx.get(f"{BASE}/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json=load_fixture_json("netbox", "devices.json"))
    )
    adapter = _adapter()
    try:
        rows = await adapter.netbox_list_devices(role="server", limit=9999)
    finally:
        await adapter.aclose()

    assert {r["name"] for r in rows} == {"test-switch-01", "test-server-01"}
    sent = route.calls.last.request.url
    assert sent.params["role"] == "server"
    assert sent.params["limit"] == "200"  # clamped from 9999


@respx.mock
async def test_device_by_ip() -> None:
    respx.get(f"{BASE}/api/ipam/ip-addresses/").mock(
        return_value=httpx.Response(200, json=load_fixture_json("netbox", "ip_addresses.json"))
    )
    adapter = _adapter()
    try:
        result = await adapter.netbox_device_by_ip(ip="192.0.2.12")
    finally:
        await adapter.aclose()

    assert result["address"] == "192.0.2.12/24"
    assert result["device"] == "test-server-01"
    assert result["dns_name"] == "test-server-01.example.invalid"


@respx.mock
async def test_device_by_ip_not_found() -> None:
    respx.get(f"{BASE}/api/ipam/ip-addresses/").mock(
        return_value=httpx.Response(200, json={"count": 0, "results": []})
    )
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="no NetBox IP-address record"):
            await adapter.netbox_device_by_ip(ip="198.51.100.99")
    finally:
        await adapter.aclose()


@respx.mock
async def test_ip_usage_computes_utilisation() -> None:
    respx.get(f"{BASE}/api/ipam/prefixes/").mock(
        return_value=httpx.Response(200, json=load_fixture_json("netbox", "prefix.json"))
    )
    respx.get(f"{BASE}/api/ipam/ip-addresses/").mock(
        return_value=httpx.Response(200, json={"count": 64, "results": []})
    )
    adapter = _adapter()
    try:
        usage = await adapter.netbox_ip_usage(prefix="192.0.2.0/24")
    finally:
        await adapter.aclose()

    assert usage["family"] == 4
    assert usage["size"] == 256
    assert usage["used"] == 64
    assert usage["free"] == 192
    assert usage["utilization_pct"] == 25.0
    assert usage["vlan"] == "test-vlan-10"


async def test_ip_usage_rejects_non_cidr() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="not a CIDR prefix"):
            await adapter.netbox_ip_usage(prefix="192.0.2.0")
    finally:
        await adapter.aclose()


@respx.mock
async def test_probe_reports_count() -> None:
    respx.get(f"{BASE}/api/dcim/devices/").mock(
        return_value=httpx.Response(200, json={"count": 42, "results": []})
    )
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert result.ok
    assert "42" in result.detail
