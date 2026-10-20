"""Proxmox VE adapter — read-only cluster, node and guest state.

Backing calls are all ``GET`` against the Proxmox VE API using an API *token*
(``Authorization: PVEAPIToken=<id>=<secret>``). The token should carry only the
audit roles (``Sys.Audit``, ``VM.Audit``, ``Datastore.Audit``); this adapter
never needs more.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..errors import HomelabMcpError
from ._http import build_client, get_json
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec, int_arg

_GUEST_TYPES = {"qemu", "lxc"}
#: Proxmox node names — validated before interpolation into an API path.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _num(value: Any) -> float | int | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _data_list(payload: Any) -> list[Any]:
    """Proxmox's ``data`` envelope field, coerced to a list.

    A restricted (audit-only) token or an empty result makes Proxmox return an
    explicit ``{"data": null}``, so ``payload.get("data", [])`` would yield
    ``None`` — not the default — and the caller would crash iterating it.
    """
    inner = payload.get("data") if isinstance(payload, dict) else None
    return inner if isinstance(inner, list) else []


def _data_dict(payload: Any) -> dict[str, Any]:
    """Proxmox's ``data`` envelope field, coerced to a dict (see :func:`_data_list`)."""
    inner = payload.get("data") if isinstance(payload, dict) else None
    return inner if isinstance(inner, dict) else {}


def _pct(used: Any, total: Any) -> float | None:
    u, t = _num(used), _num(total)
    if u is None or not t:
        return None
    return round(100.0 * u / t, 1)


def _summarise_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "node": node.get("node"),
        "status": node.get("status"),
        "cpu_pct": round(v * 100, 1) if (v := _num(node.get("cpu"))) is not None else None,
        "cpu_count": _num(node.get("maxcpu")),
        "mem_pct": _pct(node.get("mem"), node.get("maxmem")),
        "mem_used_bytes": _num(node.get("mem")),
        "mem_total_bytes": _num(node.get("maxmem")),
        "uptime_seconds": _num(node.get("uptime")),
    }


def _summarise_guest(guest: dict[str, Any]) -> dict[str, Any]:
    return {
        "vmid": _num(guest.get("vmid")),
        "name": guest.get("name"),
        "type": guest.get("type"),
        "node": guest.get("node"),
        "status": guest.get("status"),
        "cpu_pct": round(v * 100, 1) if (v := _num(guest.get("cpu"))) is not None else None,
        "cpu_count": _num(guest.get("maxcpu")),
        "mem_pct": _pct(guest.get("mem"), guest.get("maxmem")),
        "mem_used_bytes": _num(guest.get("mem")),
        "mem_total_bytes": _num(guest.get("maxmem")),
        "disk_total_bytes": _num(guest.get("maxdisk")),
        "uptime_seconds": _num(guest.get("uptime")),
    }


class ProxmoxAdapter(Adapter):
    name = "proxmox"
    required_options = ("base_url", "token_id_env", "token_secret_env")

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base_url = str(self.options.require("base_url"))
            token_id = self.options.secret("token_id_env")
            token_secret = self.options.secret("token_secret_env")
            self._client = build_client(
                base_url,
                headers={"Authorization": f"PVEAPIToken={token_id}={token_secret}"},
                verify_tls=bool(self.options.get("verify_tls", True)),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> ProbeResult:
        try:
            data = await get_json(self._get_client(), "/api2/json/version")
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))
        version = _data_dict(data).get("version", "?")
        return ProbeResult(self.name, True, f"Proxmox VE {version}")

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "proxmox_list_nodes",
                "List Proxmox VE cluster nodes with status, CPU% and memory%.",
                self.proxmox_list_nodes,
            ),
            ToolSpec(
                "proxmox_list_guests",
                "List VMs (qemu) and containers (lxc) across the cluster, "
                "optionally filtered by node or type.",
                self.proxmox_list_guests,
            ),
            ToolSpec(
                "proxmox_guest_status",
                "Get current status and resource usage for one VM or container by vmid.",
                self.proxmox_guest_status,
            ),
            ToolSpec(
                "proxmox_node_resources",
                "Get CPU load, memory and root filesystem usage for one node.",
                self.proxmox_node_resources,
            ),
        ]

    # -- tool handlers ----------------------------------------------------

    async def proxmox_list_nodes(self) -> list[dict[str, Any]]:
        """List Proxmox VE cluster nodes with status, CPU and memory usage."""

        async def _call() -> Any:
            return await get_json(self._get_client(), "/api2/json/nodes")

        data = await self.options.cache.get_or_call(("proxmox", "nodes"), _call)
        return [_summarise_node(n) for n in _data_list(data) if isinstance(n, dict)]

    async def proxmox_list_guests(
        self, node: str | None = None, guest_type: str | None = None
    ) -> list[dict[str, Any]]:
        """List VMs and containers across the cluster.

        Args:
            node: restrict to a single node name.
            guest_type: 'qemu' for VMs or 'lxc' for containers; omit for both.
        """
        if guest_type is not None and guest_type not in _GUEST_TYPES:
            raise HomelabMcpError("guest_type must be 'qemu', 'lxc', or omitted")

        guests = await self._cluster_guests()
        result = []
        for g in guests:
            if node is not None and g.get("node") != node:
                continue
            if guest_type is not None and g.get("type") != guest_type:
                continue
            result.append(_summarise_guest(g))
        return result

    async def proxmox_guest_status(self, vmid: int, node: str | None = None) -> dict[str, Any]:
        """Get current status and resource usage for one VM or container.

        Args:
            vmid: the guest's numeric id.
            node: ignored — the node and type are always resolved from cluster
                resources by vmid, so a caller cannot steer the request path.
        """
        vmid = int_arg(vmid, "vmid", lo=0, hi=2**31)
        node = None
        gtype = None
        for g in await self._cluster_guests():
            if _num(g.get("vmid")) == vmid:
                node, gtype = g.get("node"), g.get("type")
                break
        if node is None:
            raise HomelabMcpError(f"vmid {vmid} not found in cluster resources")
        if gtype not in _GUEST_TYPES or not _NAME_RE.match(str(node)):
            raise HomelabMcpError(f"cluster resources returned an unexpected node/type for {vmid}")

        path = f"/api2/json/nodes/{node}/{gtype}/{vmid}/status/current"
        data = await get_json(self._get_client(), path)
        body = _data_dict(data)
        summary = _summarise_guest({**body, "vmid": vmid, "node": node, "type": gtype})
        summary["ha_managed"] = (
            bool(body.get("ha", {}).get("managed")) if isinstance(body.get("ha"), dict) else None
        )
        return summary

    async def proxmox_node_resources(self, node: str) -> dict[str, Any]:
        """Get CPU load, memory and root filesystem usage for one node.

        Args:
            node: the node name.
        """
        if not _NAME_RE.match(node):
            raise HomelabMcpError(f"node name {node!r} contains disallowed characters")
        data = await get_json(self._get_client(), f"/api2/json/nodes/{node}/status")
        body = _data_dict(data)
        mem = body.get("memory", {}) if isinstance(body.get("memory"), dict) else {}
        rootfs = body.get("rootfs", {}) if isinstance(body.get("rootfs"), dict) else {}
        loadavg = body.get("loadavg") if isinstance(body.get("loadavg"), list) else None
        return {
            "node": node,
            "cpu_pct": round(v * 100, 1) if (v := _num(body.get("cpu"))) is not None else None,
            "loadavg": loadavg,
            "mem_pct": _pct(mem.get("used"), mem.get("total")),
            "mem_used_bytes": _num(mem.get("used")),
            "mem_total_bytes": _num(mem.get("total")),
            "rootfs_pct": _pct(rootfs.get("used"), rootfs.get("total")),
            "rootfs_used_bytes": _num(rootfs.get("used")),
            "rootfs_total_bytes": _num(rootfs.get("total")),
            "uptime_seconds": _num(body.get("uptime")),
            "kernel_version": body.get("kversion"),
        }

    # -- internals ------------------------------------------------------

    async def _cluster_guests(self) -> list[dict[str, Any]]:
        async def _call() -> Any:
            return await get_json(
                self._get_client(), "/api2/json/cluster/resources", params={"type": "vm"}
            )

        data = await self.options.cache.get_or_call(("proxmox", "cluster_guests"), _call)
        return [r for r in _data_list(data) if isinstance(r, dict)]
