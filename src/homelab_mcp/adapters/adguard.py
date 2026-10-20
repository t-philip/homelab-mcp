"""AdGuard Home adapter — read-only DNS stats, status and query log.

Uses HTTP Basic auth against the AdGuard Home control API.
"""

from __future__ import annotations

from typing import Any

import httpx

from ..errors import HomelabMcpError
from ._http import build_client, get_json
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec, int_arg

_MAX_ROWS = 100


def _top_list(raw: Any, limit: int) -> list[dict[str, Any]]:
    """AdGuard 'top_*' arrays are ``[{"name": count}, ...]``."""
    out: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw[:limit]:
            if isinstance(item, dict) and item:
                name, count = next(iter(item.items()))
                out.append({"name": name, "count": count})
    return out


class AdguardAdapter(Adapter):
    name = "adguard"
    required_options = ("base_url", "username_env", "password_env")

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base_url = str(self.options.require("base_url"))
            user = self.options.secret("username_env")
            password = self.options.secret("password_env")
            client = build_client(
                base_url,
                headers={"Accept": "application/json"},
                verify_tls=bool(self.options.get("verify_tls", True)),
            )
            client.auth = httpx.BasicAuth(user, password)
            self._client = client
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> ProbeResult:
        try:
            data = await get_json(self._get_client(), "/control/status")
            body = data if isinstance(data, dict) else {}
            running = body.get("running")
            protection = body.get("protection_enabled")
            return ProbeResult(
                self.name,
                bool(running),
                f"AdGuard {body.get('version', '?')} "
                f"(running={running}, protection_enabled={protection})",
            )
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "adguard_status",
                "AdGuard Home status: version, running, protection_enabled, DNS addresses.",
                self.adguard_status,
            ),
            ToolSpec(
                "adguard_stats",
                "AdGuard Home aggregate stats: total / blocked query counts, "
                "average processing time.",
                self.adguard_stats,
            ),
            ToolSpec(
                "adguard_top_clients",
                "Top DNS clients by query volume in the current stats window.",
                self.adguard_top_clients,
            ),
            ToolSpec(
                "adguard_top_domains",
                "Top queried and top blocked domains in the current stats window.",
                self.adguard_top_domains,
            ),
            ToolSpec(
                "adguard_query_log",
                "Recent DNS query-log entries, optionally filtered by a search string.",
                self.adguard_query_log,
            ),
        ]

    # -- tool handlers ----------------------------------------------

    async def _stats(self) -> dict[str, Any]:
        async def _call() -> Any:
            return await get_json(self._get_client(), "/control/stats")

        data = await self.options.cache.get_or_call(("adguard", "stats"), _call)
        return data if isinstance(data, dict) else {}

    async def adguard_status(self) -> dict[str, Any]:
        """AdGuard Home status."""
        data = await get_json(self._get_client(), "/control/status")
        body = data if isinstance(data, dict) else {}
        return {
            "version": body.get("version"),
            "running": body.get("running"),
            "protection_enabled": body.get("protection_enabled"),
            "dns_addresses": body.get("dns_addresses"),
            "dhcp_available": body.get("dhcp_available"),
        }

    async def adguard_stats(self) -> dict[str, Any]:
        """AdGuard Home aggregate query stats."""
        s = await self._stats()
        return {
            "num_dns_queries": s.get("num_dns_queries"),
            "num_blocked_filtering": s.get("num_blocked_filtering"),
            "num_replaced_safebrowsing": s.get("num_replaced_safebrowsing"),
            "num_replaced_parental": s.get("num_replaced_parental"),
            "avg_processing_time_ms": (
                round(v * 1000, 2)
                if isinstance((v := s.get("avg_processing_time")), (int, float))
                else None
            ),
            "time_units": s.get("time_units"),
        }

    async def adguard_top_clients(self, limit: int = 10) -> list[dict[str, Any]]:
        """Top DNS clients by query count.

        Args:
            limit: how many to return (1-100).
        """
        limit = int_arg(limit, "limit", lo=1, hi=_MAX_ROWS)
        return _top_list((await self._stats()).get("top_clients"), limit)

    async def adguard_top_domains(self, limit: int = 10) -> dict[str, list[dict[str, Any]]]:
        """Top queried and top blocked domains.

        Args:
            limit: how many of each to return (1-100).
        """
        limit = int_arg(limit, "limit", lo=1, hi=_MAX_ROWS)
        s = await self._stats()
        return {
            "queried": _top_list(s.get("top_queried_domains"), limit),
            "blocked": _top_list(s.get("top_blocked_domains"), limit),
        }

    async def adguard_query_log(
        self, limit: int = 20, search: str | None = None
    ) -> list[dict[str, Any]]:
        """Recent DNS query-log entries.

        Args:
            limit: how many entries to return (1-100).
            search: optional substring filter (domain or client).
        """
        limit = int_arg(limit, "limit", lo=1, hi=_MAX_ROWS)
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        data = await get_json(self._get_client(), "/control/querylog", params=params)
        body = data if isinstance(data, dict) else {}
        entries = body.get("data", []) if isinstance(body.get("data"), list) else []
        out: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            question = entry.get("question", {}) if isinstance(entry.get("question"), dict) else {}
            out.append(
                {
                    "time": entry.get("time"),
                    "client": entry.get("client"),
                    "name": question.get("name"),
                    "type": question.get("type"),
                    "status": entry.get("reason"),
                    "filtered": entry.get("filtered"),
                    "elapsed_ms": entry.get("elapsedMs"),
                    "upstream": entry.get("upstream"),
                }
            )
        return out
