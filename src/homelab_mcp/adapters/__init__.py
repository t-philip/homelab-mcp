"""Adapters — one per backing system, all read-only."""

from __future__ import annotations

from .adguard import AdguardAdapter
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec
from .grafana import GrafanaAdapter
from .http_json import HttpJsonAdapter
from .influxdb import InfluxdbAdapter
from .netbox import NetboxAdapter
from .pinglog import PinglogAdapter
from .proxmox import ProxmoxAdapter
from .systemd import SystemdAdapter

#: Adapter key -> class. The registry consults this when building a server.
ADAPTER_CLASSES: dict[str, type[Adapter]] = {
    ProxmoxAdapter.name: ProxmoxAdapter,
    NetboxAdapter.name: NetboxAdapter,
    SystemdAdapter.name: SystemdAdapter,
    GrafanaAdapter.name: GrafanaAdapter,
    InfluxdbAdapter.name: InfluxdbAdapter,
    AdguardAdapter.name: AdguardAdapter,
    PinglogAdapter.name: PinglogAdapter,
    HttpJsonAdapter.name: HttpJsonAdapter,
}


def iter_declared_tools() -> list[ToolSpec]:
    """Every tool every registered adapter declares.

    Construction is cheap and never raises (config shape is checked separately by
    ``Adapter.validate``), so an empty ``raw`` is enough to enumerate tool names
    for a lint or a test. No endpoint is contacted and no credential is read.
    """
    from ..cache import TTLCache

    options = AdapterOptions(raw={}, secrets={}, cache=TTLCache(0))
    specs: list[ToolSpec] = []
    for cls in ADAPTER_CLASSES.values():
        specs.extend(cls(options).tools())
    return specs


__all__ = [
    "ADAPTER_CLASSES",
    "Adapter",
    "AdapterOptions",
    "AdguardAdapter",
    "GrafanaAdapter",
    "HttpJsonAdapter",
    "InfluxdbAdapter",
    "NetboxAdapter",
    "PinglogAdapter",
    "ProbeResult",
    "ProxmoxAdapter",
    "SystemdAdapter",
    "ToolSpec",
    "iter_declared_tools",
]
