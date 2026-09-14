"""Grafana adapter — read-only dashboards, alert-rule state and health.

Uses a Grafana service-account (or API) token: ``Authorization: Bearer <token>``.
A Viewer-role token is enough for everything here.
"""

from __future__ import annotations

import re
from typing import Any

import httpx

from ..errors import HomelabMcpError, UpstreamError
from ._http import build_client, get_json
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec

_ALERT_STATES = {"firing", "pending", "inactive", "normal", "alerting"}
_UID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _dashboard_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "uid": row.get("uid"),
        "title": row.get("title"),
        "folder": row.get("folderTitle") or "General",
        "tags": row.get("tags") or [],
        "url": row.get("url"),
    }


class GrafanaAdapter(Adapter):
    name = "grafana"
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
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                verify_tls=bool(self.options.get("verify_tls", True)),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def probe(self) -> ProbeResult:
        try:
            data = await get_json(self._get_client(), "/api/health")
            version = data.get("version", "?") if isinstance(data, dict) else "?"
            db = data.get("database", "?") if isinstance(data, dict) else "?"
            return ProbeResult(self.name, True, f"Grafana {version} (database: {db})")
        except HomelabMcpError as exc:
            return ProbeResult(self.name, False, str(exc))

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "grafana_list_dashboards",
                "List Grafana dashboards (uid, title, folder, tags), optionally "
                "filtered by a title query or a tag.",
                self.grafana_list_dashboards,
            ),
            ToolSpec(
                "grafana_dashboard",
                "Get one dashboard by uid: its title, folder, and the list of panel titles.",
                self.grafana_dashboard,
            ),
            ToolSpec(
                "grafana_list_alerts",
                "List Grafana-managed alert rules and their current state, "
                "optionally filtered by state (firing/pending/normal/...).",
                self.grafana_list_alerts,
            ),
            ToolSpec(
                "grafana_health",
                "Grafana server health: version and database status.",
                self.grafana_health,
            ),
        ]

    # -- tool handlers ------------------------------------------------

    async def grafana_list_dashboards(
        self, query: str | None = None, tag: str | None = None
    ) -> list[dict[str, Any]]:
        """List dashboards.

        Args:
            query: case-insensitive substring to match against the title.
            tag: return only dashboards carrying this tag.
        """
        params: dict[str, Any] = {"type": "dash-db", "limit": 500}
        if query:
            params["query"] = query
        if tag:
            params["tag"] = tag

        async def _call() -> Any:
            return await get_json(self._get_client(), "/api/search", params=params)

        key = ("grafana", "search", query or "", tag or "")
        data = await self.options.cache.get_or_call(key, _call)
        rows = data if isinstance(data, list) else []
        return [_dashboard_row(r) for r in rows if isinstance(r, dict)]

    async def grafana_dashboard(self, uid: str) -> dict[str, Any]:
        """Get a single dashboard's metadata and panel titles.

        Args:
            uid: the dashboard uid.
        """
        if not _UID_RE.match(uid):
            raise HomelabMcpError(f"dashboard uid {uid!r} contains disallowed characters")
        data = await get_json(self._get_client(), f"/api/dashboards/uid/{uid}")
        body = data if isinstance(data, dict) else {}
        dash = body.get("dashboard", {}) if isinstance(body.get("dashboard"), dict) else {}
        meta = body.get("meta", {}) if isinstance(body.get("meta"), dict) else {}
        panels = dash.get("panels", []) if isinstance(dash.get("panels"), list) else []
        return {
            "uid": dash.get("uid") or uid,
            "title": dash.get("title"),
            "folder": meta.get("folderTitle") or "General",
            "tags": dash.get("tags") or [],
            "panel_count": len(panels),
            "panels": [p.get("title") for p in panels if isinstance(p, dict) and p.get("title")],
        }

    async def grafana_list_alerts(self, state: str | None = None) -> list[dict[str, Any]]:
        """List Grafana-managed alert rules and their state.

        Args:
            state: optional filter — 'firing', 'pending', 'normal', 'inactive'.
        """
        if state is not None and state not in _ALERT_STATES:
            raise HomelabMcpError(
                f"state must be one of {', '.join(sorted(_ALERT_STATES))} or omitted"
            )

        async def _call() -> Any:
            return await get_json(self._get_client(), "/api/prometheus/grafana/api/v1/rules")

        data = await self.options.cache.get_or_call(("grafana", "rules"), _call)
        if isinstance(data, dict) and data.get("status") not in (None, "success"):
            raise UpstreamError(f"Grafana rules API: {data.get('error') or data.get('status')}")
        body = data.get("data", {}) if isinstance(data, dict) else {}
        groups = body.get("groups", []) if isinstance(body.get("groups"), list) else []
        out: list[dict[str, Any]] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            for rule in group.get("rules", []):
                if not isinstance(rule, dict):
                    continue
                rule_state = rule.get("state")
                if state is not None and rule_state != state:
                    continue
                active = rule.get("alerts", []) if isinstance(rule.get("alerts"), list) else []
                out.append(
                    {
                        "name": rule.get("name"),
                        "group": group.get("name"),
                        "folder": group.get("file"),
                        "state": rule_state,
                        "health": rule.get("health"),
                        "active_alerts": len(active),
                        "last_evaluation": rule.get("lastEvaluation"),
                    }
                )
        return out

    async def grafana_health(self) -> dict[str, Any]:
        """Grafana server health."""
        data = await get_json(self._get_client(), "/api/health")
        body = data if isinstance(data, dict) else {}
        return {
            "version": body.get("version"),
            "database": body.get("database"),
            "commit": body.get("commit"),
        }
