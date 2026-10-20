"""Generic HTTP-JSON adapter — GET a preconfigured endpoint and return its JSON.

The model can only name an endpoint that the operator has already defined in the
config; it cannot supply an arbitrary URL (no SSRF surface). Each endpoint may
carry static headers, a bearer-token env var, and an optional ``extract`` path
that pulls one sub-value out of the response.

    [adapters.http_json]
    enabled = true

      [adapters.http_json.endpoints.service_health]
      url = "https://service.example.invalid/api/health"
      token_env = "SERVICE_TOKEN"       # -> Authorization: Bearer <value>
      headers = { Accept = "application/json" }
      extract = "status"                 # dotted path, e.g. "a.b.0.c"
      verify_tls = true
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from ..errors import ConfigError, HomelabMcpError, UpstreamError
from ._http import build_client, read_capped
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec

_LOG = logging.getLogger("homelab_mcp.http_json")


def _resolve_path(value: Any, path: str) -> Any:
    """Walk a dotted path into nested dicts/lists. ``a.b.0.c``."""
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise HomelabMcpError(f"extract path {path!r}: no key {part!r}")
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise HomelabMcpError(f"extract path {path!r}: bad index {part!r}") from exc
        else:
            raise HomelabMcpError(
                f"extract path {path!r}: cannot descend into {type(current).__name__}"
            )
    return current


class _Endpoint:
    __slots__ = ("extract", "headers", "name", "token_env", "url", "verify_tls")

    def __init__(self, name: str, raw: dict[str, Any]) -> None:
        if not isinstance(raw, dict) or "url" not in raw:
            raise ConfigError(f"[adapters.http_json.endpoints.{name}] needs a 'url'")
        self.name = name
        self.url = str(raw["url"])
        try:
            parsed = httpx.URL(self.url)
        except httpx.InvalidURL as exc:
            raise ConfigError(
                f"[adapters.http_json.endpoints.{name}] url {self.url!r} is not a valid URL: {exc}"
            ) from exc
        if parsed.scheme not in ("http", "https") or not parsed.host:
            raise ConfigError(
                f"[adapters.http_json.endpoints.{name}] url must be an absolute http(s) URL"
            )
        self.headers = {str(k): str(v) for k, v in (raw.get("headers") or {}).items()}
        self.token_env = raw.get("token_env")
        self.extract = raw.get("extract")
        self.verify_tls = bool(raw.get("verify_tls", True))


class HttpJsonAdapter(Adapter):
    name = "http_json"
    required_options = ("endpoints",)

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        raw_endpoints = options.get("endpoints") or {}
        endpoints = raw_endpoints if isinstance(raw_endpoints, dict) else {}
        self._endpoints = {name: _Endpoint(name, body) for name, body in endpoints.items()}
        self._clients: dict[str, httpx.AsyncClient] = {}

    @classmethod
    def validate(cls, options: AdapterOptions) -> None:
        super().validate(options)
        raw_endpoints = options.get("endpoints")
        if not isinstance(raw_endpoints, dict) or not raw_endpoints:
            raise ConfigError("[adapters.http_json].endpoints must be a non-empty table")
        for name, body in raw_endpoints.items():
            _Endpoint(str(name), body)  # raises ConfigError on a bad entry

    def _client_for(self, ep: _Endpoint) -> httpx.AsyncClient:
        if ep.name not in self._clients:
            headers = dict(ep.headers)
            if ep.token_env:
                token = self.options.secrets.get(str(ep.token_env))
                if not token:
                    raise HomelabMcpError(
                        f"endpoint {ep.name!r}: env var ${ep.token_env} is not set"
                    )
                headers.setdefault("Authorization", f"Bearer {token}")
            self._clients[ep.name] = build_client(ep.url, headers=headers, verify_tls=ep.verify_tls)
        return self._clients[ep.name]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    async def probe(self) -> ProbeResult:
        if not self._endpoints:
            return ProbeResult(self.name, False, "no endpoints configured")
        return ProbeResult(
            self.name,
            True,
            f"{len(self._endpoints)} endpoint(s): {', '.join(sorted(self._endpoints))}",
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "http_json_list_endpoints",
                "List the configured HTTP-JSON endpoints by name.",
                self.http_json_list_endpoints,
            ),
            ToolSpec(
                "http_json_get",
                "GET one preconfigured HTTP-JSON endpoint by name and return its "
                "parsed JSON (or the configured extracted sub-value).",
                self.http_json_get,
            ),
        ]

    # -- tool handlers ------------------------------------------

    async def http_json_list_endpoints(self) -> list[dict[str, Any]]:
        """List configured endpoints (name and URL, no secrets)."""
        return [
            {"name": ep.name, "url": ep.url, "extract": ep.extract}
            for ep in sorted(self._endpoints.values(), key=lambda e: e.name)
        ]

    async def http_json_get(self, name: str) -> Any:
        """Fetch one configured endpoint.

        Args:
            name: the endpoint name from the config.
        """
        ep = self._endpoints.get(name)
        if ep is None:
            raise HomelabMcpError(
                f"unknown endpoint {name!r} (have: {', '.join(sorted(self._endpoints)) or 'none'})"
            )

        async def _call() -> Any:
            client = self._client_for(ep)
            try:
                async with client.stream("GET", ep.url) as response:
                    if response.status_code >= 400:
                        body = (await read_capped(response)).decode("utf-8", "replace")
                        _LOG.warning(
                            "GET %s -> HTTP %s: %s", ep.name, response.status_code, body[:500]
                        )
                        raise UpstreamError(f"GET {ep.name}: HTTP {response.status_code}")
                    raw = await read_capped(response)
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise UpstreamError(f"GET {ep.name} failed: {exc}") from exc
            try:
                return json.loads(raw)
            except ValueError as exc:
                raise UpstreamError(f"GET {ep.name}: response was not JSON") from exc

        data = await self.options.cache.get_or_call(("http_json", name), _call)
        if ep.extract:
            return _resolve_path(data, str(ep.extract))
        return data
