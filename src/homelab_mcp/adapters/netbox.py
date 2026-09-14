"""NetBox adapter — read-only device and IP-address lookups.

Uses a NetBox API token (``Authorization: Token <token>``). A read-only token is
sufficient and recommended.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any

import httpx

from ..errors import HomelabMcpError
from ._http import build_client, get_json
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec, int_arg

_MAX_LIMIT = 200
_FIND_LIMIT = 200
_PREFIX_RE = re.compile(r"^[0-9a-fA-F:.]+/\d{1,3}$")


def _label(obj: Any) -> Any:
    """NetBox nested choice/related objects render as ``{'value'|'name'|'label': ...}``."""
    if isinstance(obj, dict):
        return obj.get("label") or obj.get("name") or obj.get("value") or obj.get("display")
    return obj


def _device_type(dt: Any) -> str | None:
    if not isinstance(dt, dict):
        return None
    manufacturer = dt.get("manufacturer")
    mname = manufacturer.get("name") if isinstance(manufacturer, dict) else None
    model = dt.get("model") or dt.get("display")
    return f"{mname} {model}".strip() if mname else model


def _summarise_device(dev: dict[str, Any]) -> dict[str, Any]:
    role = dev.get("role") or dev.get("device_role")
    primary4 = dev.get("primary_ip4") or {}
    primary6 = dev.get("primary_ip6") or {}
    return {
        "id": dev.get("id"),
        "name": dev.get("name") or dev.get("display"),
        "status": _label(dev.get("status")),
        "role": _label(role),
        "device_type": _device_type(dev.get("device_type")),
        "site": _label(dev.get("site")),
        "primary_ip4": primary4.get("address") if isinstance(primary4, dict) else None,
        "primary_ip6": primary6.get("address") if isinstance(primary6, dict) else None,
        "serial": dev.get("serial") or None,
        "url": dev.get("url"),
    }


class NetboxAdapter(Adapter):
    name = "netbox"
    required_options = ("base_url", "token_env")

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base_url = str(self.options.require("base_url"))
            token = self.options.secret("token_env")
            self._client = build_client(
                base_url,
                headers={"Authorization": f"Token {token}", "Accept": "application/json"},
                verify_tls=bool(self.options.get("verify_tls", True)),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> ProbeResult:
        try:
            data = await get_json(self._get_client(), "/api/dcim/devices/", params={"limit": 1})
            count = data.get("count") if isinstance(data, dict) else None
            return ProbeResult(self.name, True, f"NetBox reachable ({count} devices)")
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "netbox_find_device",
                "Find NetBox devices whose name matches exactly (case-insensitive).",
                self.netbox_find_device,
            ),
            ToolSpec(
                "netbox_list_devices",
                "List NetBox devices, optionally filtered by role, site or status.",
                self.netbox_list_devices,
            ),
            ToolSpec(
                "netbox_device_by_ip",
                "Look up which NetBox device (or other object) holds a given IP address.",
                self.netbox_device_by_ip,
            ),
            ToolSpec(
                "netbox_ip_usage",
                "Report address utilisation for a NetBox prefix (size, used, free, percent).",
                self.netbox_ip_usage,
            ),
        ]

    # -- tool handlers --------------------------------------------------

    async def netbox_find_device(self, name: str) -> list[dict[str, Any]]:
        """Find NetBox devices by exact name.

        Args:
            name: the device name to match.
        """
        rows = await self._get_devices({"name__ie": name, "limit": _FIND_LIMIT})
        if not rows:
            rows = await self._get_devices({"name": name, "limit": _FIND_LIMIT})
        return [_summarise_device(d) for d in rows]

    async def netbox_list_devices(
        self,
        role: str | None = None,
        site: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List NetBox devices with optional filters.

        Args:
            role: device role slug (e.g. 'server', 'switch').
            site: site slug.
            status: status value (e.g. 'active', 'offline').
            limit: maximum rows to return (1-200).
        """
        params: dict[str, Any] = {"limit": int_arg(limit, "limit", lo=1, hi=_MAX_LIMIT)}
        if role:
            params["role"] = role
        if site:
            params["site"] = site
        if status:
            params["status"] = status
        rows = await self._get_devices(params)
        return [_summarise_device(d) for d in rows]

    async def netbox_device_by_ip(self, ip: str) -> dict[str, Any]:
        """Find the object assigned a given IP address.

        Args:
            ip: an IP address, with or without a CIDR suffix.
        """

        async def _call() -> Any:
            return await get_json(
                self._get_client(), "/api/ipam/ip-addresses/", params={"address": ip}
            )

        data = await self.options.cache.get_or_call(("netbox", "ip", ip), _call)
        results = data.get("results", []) if isinstance(data, dict) else []
        if not results:
            raise HomelabMcpError(f"no NetBox IP-address record for {ip}")
        record = results[0]
        assigned = record.get("assigned_object")
        assigned_name: str | None = None
        device_name: str | None = None
        if isinstance(assigned, dict):
            assigned_name = assigned.get("display") or assigned.get("name")
            device = assigned.get("device")
            if isinstance(device, dict):
                device_name = device.get("name")
        return {
            "address": record.get("address"),
            "status": _label(record.get("status")),
            "dns_name": record.get("dns_name") or None,
            "assigned_object_type": record.get("assigned_object_type"),
            "assigned_object": assigned_name,
            "device": device_name,
        }

    async def netbox_ip_usage(self, prefix: str) -> dict[str, Any]:
        """Report address utilisation for a prefix.

        Args:
            prefix: a CIDR prefix, e.g. '192.0.2.0/24'.
        """
        if not _PREFIX_RE.match(prefix):
            raise HomelabMcpError(f"prefix {prefix!r} is not a CIDR prefix")
        try:
            net = ipaddress.ip_network(prefix, strict=False)
        except ValueError as exc:
            raise HomelabMcpError(f"prefix {prefix!r} is not a valid network") from exc

        async def _prefix() -> Any:
            return await get_json(
                self._get_client(), "/api/ipam/prefixes/", params={"prefix": prefix}
            )

        async def _ips() -> Any:
            return await get_json(
                self._get_client(),
                "/api/ipam/ip-addresses/",
                params={"parent": prefix, "limit": 1},
            )

        pdata = await self.options.cache.get_or_call(("netbox", "prefix", prefix), _prefix)
        results = pdata.get("results", []) if isinstance(pdata, dict) else []
        if not results:
            raise HomelabMcpError(f"no NetBox prefix record for {prefix}")
        record = results[0]

        ipdata = await self.options.cache.get_or_call(("netbox", "prefix_ips", prefix), _ips)
        used = ipdata.get("count") if isinstance(ipdata, dict) else None
        size = net.num_addresses
        util = (
            round(100.0 * used / size, 1)
            if isinstance(used, int) and not isinstance(used, bool) and size
            else None
        )
        return {
            "prefix": record.get("prefix") or prefix,
            "family": net.version,
            "size": size,
            "used": used,
            "free": (size - used) if isinstance(used, int) and not isinstance(used, bool) else None,
            "utilization_pct": util,
            "status": _label(record.get("status")),
            "vlan": _label(record.get("vlan")),
            "role": _label(record.get("role")),
            "site": _label(record.get("site")),
            "description": record.get("description") or None,
        }

    # -- internals ----------------------------------------------------

    async def _get_devices(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        key = ("netbox", "devices", tuple(sorted(params.items())))

        async def _call() -> Any:
            return await get_json(self._get_client(), "/api/dcim/devices/", params=params)

        data = await self.options.cache.get_or_call(key, _call)
        rows = data.get("results", []) if isinstance(data, dict) else []
        return [r for r in rows if isinstance(r, dict)]
