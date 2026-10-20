from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters.proxmox import ProxmoxAdapter
from homelab_mcp.cache import TTLCache
from homelab_mcp.errors import HomelabMcpError

from .conftest import load_fixture_json, make_options

BASE = "https://pve.example.invalid:8006"


def _adapter(cache: TTLCache | None = None) -> ProxmoxAdapter:
    return ProxmoxAdapter(
        make_options(
            raw={
                "base_url": BASE,
                "token_id_env": "PVE_ID",
                "token_secret_env": "PVE_SECRET",
                "verify_tls": True,
            },
            secrets={"PVE_ID": "homelab-mcp@pve!ro", "PVE_SECRET": "shhh"},
            cache=cache,
        )
    )


@respx.mock
async def test_list_nodes_summarises_and_caches() -> None:
    route = respx.get(f"{BASE}/api2/json/nodes").mock(
        return_value=httpx.Response(200, json=load_fixture_json("proxmox", "nodes.json"))
    )
    adapter = _adapter()
    try:
        first = await adapter.proxmox_list_nodes()
        second = await adapter.proxmox_list_nodes()
    finally:
        await adapter.aclose()

    assert first == second
    assert route.call_count == 1  # cached
    node_a = next(n for n in first if n["node"] == "node-a")
    assert node_a["status"] == "online"
    assert node_a["cpu_pct"] == pytest.approx(12.3)  # rounded to 1 dp
    assert node_a["mem_pct"] == pytest.approx(25.0)
    assert node_a["cpu_count"] == 8


@respx.mock
async def test_list_guests_filters() -> None:
    respx.get(f"{BASE}/api2/json/cluster/resources").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("proxmox", "cluster_resources_vm.json")
        )
    )
    adapter = _adapter()
    try:
        everything = await adapter.proxmox_list_guests()
        only_lxc = await adapter.proxmox_list_guests(guest_type="lxc")
        only_node_b = await adapter.proxmox_list_guests(node="node-b")
    finally:
        await adapter.aclose()

    assert {g["vmid"] for g in everything} == {201, 301, 302}
    assert [g["vmid"] for g in only_lxc] == [201]
    assert {g["vmid"] for g in only_node_b} == {301, 302}
    assert all(g["type"] == "lxc" for g in only_lxc)


@respx.mock
async def test_list_guests_rejects_bad_type() -> None:
    respx.get(f"{BASE}/api2/json/cluster/resources").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("proxmox", "cluster_resources_vm.json")
        )
    )
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="guest_type"):
            await adapter.proxmox_list_guests(guest_type="container")
    finally:
        await adapter.aclose()


@respx.mock
async def test_guest_status_resolves_node_automatically() -> None:
    respx.get(f"{BASE}/api2/json/cluster/resources").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("proxmox", "cluster_resources_vm.json")
        )
    )
    status_route = respx.get(f"{BASE}/api2/json/nodes/node-a/lxc/201/status/current").mock(
        return_value=httpx.Response(200, json=load_fixture_json("proxmox", "guest_status_201.json"))
    )
    adapter = _adapter()
    try:
        result = await adapter.proxmox_guest_status(vmid=201)
    finally:
        await adapter.aclose()

    assert status_route.called
    assert result["vmid"] == 201
    assert result["node"] == "node-a"
    assert result["type"] == "lxc"
    assert result["status"] == "running"
    assert result["ha_managed"] is True


@respx.mock
async def test_guest_status_unknown_vmid() -> None:
    respx.get(f"{BASE}/api2/json/cluster/resources").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("proxmox", "cluster_resources_vm.json")
        )
    )
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="not found"):
            await adapter.proxmox_guest_status(vmid=999)
    finally:
        await adapter.aclose()


async def test_node_resources_rejects_path_traversal() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="disallowed characters"):
            await adapter.proxmox_node_resources(node="../../version")
    finally:
        await adapter.aclose()


async def test_guest_status_bad_vmid_is_tool_error() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="must be an integer"):
            await adapter.proxmox_guest_status(vmid="1; DROP")  # type: ignore[arg-type]
    finally:
        await adapter.aclose()


@respx.mock
async def test_node_resources() -> None:
    respx.get(f"{BASE}/api2/json/nodes/node-a/status").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("proxmox", "node_status_node_a.json")
        )
    )
    adapter = _adapter()
    try:
        result = await adapter.proxmox_node_resources(node="node-a")
    finally:
        await adapter.aclose()

    assert result["node"] == "node-a"
    assert result["mem_pct"] == pytest.approx(25.0, rel=1e-3)
    assert result["rootfs_pct"] == pytest.approx(5.0, rel=1e-3)
    assert result["loadavg"] == ["0.42", "0.55", "0.61"]


@respx.mock
async def test_probe_ok() -> None:
    respx.get(f"{BASE}/api2/json/version").mock(
        return_value=httpx.Response(200, json=load_fixture_json("proxmox", "version.json"))
    )
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert result.ok
    assert "8.0.0" in result.detail


@respx.mock
async def test_probe_failure_is_not_raised() -> None:
    respx.get(f"{BASE}/api2/json/version").mock(return_value=httpx.Response(500, text="boom"))
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert not result.ok


@respx.mock
async def test_null_data_envelope_does_not_crash() -> None:
    """An audit-only token makes Proxmox return {"data": null} on many endpoints."""
    respx.get(f"{BASE}/api2/json/nodes").mock(return_value=httpx.Response(200, json={"data": None}))
    respx.get(f"{BASE}/api2/json/cluster/resources").mock(
        return_value=httpx.Response(200, json={"data": None})
    )
    respx.get(f"{BASE}/api2/json/nodes/node-a/status").mock(
        return_value=httpx.Response(200, json={"data": None})
    )
    adapter = _adapter()
    try:
        assert await adapter.proxmox_list_nodes() == []
        assert await adapter.proxmox_list_guests() == []
        node = await adapter.proxmox_node_resources(node="node-a")
        assert node["node"] == "node-a" and node["mem_pct"] is None
    finally:
        await adapter.aclose()
