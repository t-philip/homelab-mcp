"""InfluxDB v2 adapter — read-only, parameterised queries only.

The model never supplies raw Flux. Every tool builds a query from validated
parts: bucket / measurement / field names must match a strict pattern; a value
that reaches a filter predicate may not contain a quote, backslash, backtick,
control character, or ``$`` / ``{`` / ``}`` (which would let Flux ``${...}``
string interpolation run inside the literal); and the time range is a bounded
relative duration (``-1h`` … ``-400d``). Query results are cached like the
other adapters. This keeps the surface to "read a named series", not "run
arbitrary Flux".

``POST /api/v2/query`` is the one non-GET call in the codebase; it is a read
(``/api/v2/write`` is the write endpoint) and is on the read-only allowlist in
``homelab_mcp._audit``.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from typing import Any

import httpx

from ..errors import HomelabMcpError, UpstreamError
from ._http import build_client, get_json, read_capped
from .base import Adapter, AdapterOptions, ProbeResult, ToolSpec

_LOG = logging.getLogger("homelab_mcp.influxdb")

_IDENT_RE = re.compile(r"^[A-Za-z0-9_.:\-/]+$")
_TAGKEY_RE = re.compile(r"^[A-Za-z0-9_]+$")
_DURATION_RE = re.compile(r"^-(\d+)(ns|us|ms|s|m|h|d|w)$")
_MAX_ROWS = 500
_MAX_BUCKETS = 200
#: Reject a lookback longer than this (~13 months) — it would trigger a
#: full-history scan on a large bucket.
_MAX_LOOKBACK_SECONDS = 400 * 86400
_UNIT_SECONDS = {
    "ns": 1e-9,
    "us": 1e-6,
    "ms": 1e-3,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}
#: Anything that could start a Flux expression or string interpolation. Values
#: that reach a filter predicate are wrapped in "..." — `${...}` interpolation
#: is evaluated inside such a string, so `$` `{` `}` are rejected too.
_FLUX_FORBIDDEN = ('"', "\\", "\n", "\r", "$", "{", "}", "`")


def _check_ident(kind: str, value: str) -> str:
    if not _IDENT_RE.match(value):
        raise HomelabMcpError(f"{kind} {value!r} contains disallowed characters")
    return value


def _check_range(start: str) -> str:
    m = _DURATION_RE.match(start)
    if not m:
        raise HomelabMcpError(
            f"since {start!r} must be a negative Flux duration like '-1h', '-30m', '-7d'"
        )
    seconds = int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    if seconds > _MAX_LOOKBACK_SECONDS:
        raise HomelabMcpError(f"since {start!r} looks back too far (max ~400d)")
    return start


def _flux_string(value: str) -> str:
    if any(ch in value for ch in _FLUX_FORBIDDEN) or any(ord(ch) < 0x20 for ch in value):
        raise HomelabMcpError(f"value {value!r} contains characters not allowed in a filter")
    return f'"{value}"'


def _parse_annotated_csv(text: str) -> list[dict[str, str]]:
    """Parse InfluxDB's annotated CSV into a list of row dicts."""
    rows: list[dict[str, str]] = []
    header: list[str] | None = None
    for record in csv.reader(io.StringIO(text)):
        if not record or all(cell == "" for cell in record):
            header = None  # a blank line separates result tables
            continue
        if record[0].startswith("#"):
            continue
        if header is None:
            header = record
            continue
        rows.append(dict(zip(header, record, strict=False)))
    return rows


class InfluxdbAdapter(Adapter):
    name = "influxdb"
    required_options = ("base_url", "token_env", "org")

    def __init__(self, options: AdapterOptions) -> None:
        super().__init__(options)
        self._client: httpx.AsyncClient | None = None

    @property
    def _org(self) -> str:
        return str(self.options.require("org"))

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            base_url = str(self.options.require("base_url"))
            token = self.options.secret("token_env")
            self._client = build_client(
                base_url,
                headers={"Authorization": f"Token {token}"},
                verify_tls=bool(self.options.get("verify_tls", True)),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _query(self, flux: str) -> list[dict[str, str]]:
        async def _call() -> list[dict[str, str]]:
            client = self._get_client()
            try:
                async with client.stream(
                    "POST",
                    "/api/v2/query",
                    params={"org": self._org},
                    headers={
                        "Content-Type": "application/vnd.flux",
                        "Accept": "application/csv",
                    },
                    content=flux,
                ) as response:
                    if response.status_code >= 400:
                        detail = (await read_capped(response)).decode("utf-8", "replace")
                        _LOG.warning(
                            "InfluxDB query -> HTTP %s: %s", response.status_code, detail[:500]
                        )
                        raise UpstreamError(f"InfluxDB query returned HTTP {response.status_code}")
                    body = (await read_capped(response)).decode("utf-8", "replace")
            except httpx.HTTPError as exc:
                raise UpstreamError(f"InfluxDB query failed: {exc}") from exc
            return _parse_annotated_csv(body)

        return await self.options.cache.get_or_call(("influxdb", "query", flux), _call)

    async def probe(self) -> ProbeResult:
        try:
            response = await self._get_client().get("/health")
            if response.status_code >= 400:
                return ProbeResult(self.name, False, f"HTTP {response.status_code}")
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            return ProbeResult(self.name, False, str(exc))
        if not isinstance(body, dict):
            return ProbeResult(self.name, False, "unexpected /health response")
        return ProbeResult(
            self.name,
            body.get("status") == "pass",
            f"InfluxDB {body.get('version', '?')} ({body.get('status')})",
        )

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "influxdb_list_buckets",
                "List InfluxDB buckets in the configured org (name, id, retention).",
                self.influxdb_list_buckets,
            ),
            ToolSpec(
                "influxdb_measurements",
                "List measurement names present in a bucket over a recent window.",
                self.influxdb_measurements,
            ),
            ToolSpec(
                "influxdb_field_keys",
                "List field keys for a measurement in a bucket over a recent window.",
                self.influxdb_field_keys,
            ),
            ToolSpec(
                "influxdb_latest_value",
                "Get the most recent value(s) for a measurement/field in a bucket, "
                "optionally narrowed by exact tag matches.",
                self.influxdb_latest_value,
            ),
        ]

    # -- tool handlers --------------------------------------------

    async def influxdb_list_buckets(self) -> list[dict[str, Any]]:
        """List buckets in the configured org (first 200)."""
        data = await get_json(
            self._get_client(),
            "/api/v2/buckets",
            params={"org": self._org, "limit": _MAX_BUCKETS},
        )
        buckets = data.get("buckets", []) if isinstance(data, dict) else []
        out: list[dict[str, Any]] = []
        for b in buckets:
            if not isinstance(b, dict):
                continue
            retention = None
            rules = b.get("retentionRules")
            if isinstance(rules, list) and rules and isinstance(rules[0], dict):
                retention = rules[0].get("everySeconds")
            out.append({"name": b.get("name"), "id": b.get("id"), "retention_seconds": retention})
        return out

    async def influxdb_measurements(self, bucket: str, since: str = "-24h") -> list[str]:
        """List measurements in a bucket.

        Args:
            bucket: the bucket name.
            since: how far back to look, a negative Flux duration (default '-24h').
        """
        _check_ident("bucket", bucket)
        _check_range(since)
        flux = (
            'import "influxdata/influxdb/schema"\n'
            f"schema.measurements(bucket: {_flux_string(bucket)}, "
            f"start: {since})"
        )
        rows = await self._query(flux)
        return [r["_value"] for r in rows if r.get("_value")]

    async def influxdb_field_keys(
        self, bucket: str, measurement: str, since: str = "-24h"
    ) -> list[str]:
        """List field keys for a measurement.

        Args:
            bucket: the bucket name.
            measurement: the measurement name.
            since: how far back to look, a negative Flux duration (default '-24h').
        """
        _check_ident("bucket", bucket)
        _check_ident("measurement", measurement)
        _check_range(since)
        flux = (
            'import "influxdata/influxdb/schema"\n'
            "schema.measurementFieldKeys("
            f"bucket: {_flux_string(bucket)}, "
            f"measurement: {_flux_string(measurement)}, "
            f"start: {since})"
        )
        rows = await self._query(flux)
        return [r["_value"] for r in rows if r.get("_value")]

    async def influxdb_latest_value(
        self,
        bucket: str,
        measurement: str,
        field: str | None = None,
        tags: dict[str, str] | None = None,
        since: str = "-1h",
    ) -> list[dict[str, Any]]:
        """Get the most recent point(s) for a series.

        Args:
            bucket: the bucket name.
            measurement: the measurement name.
            field: restrict to one field key; omit for all fields.
            tags: exact tag matches, e.g. {"host": "node-a"}.
            since: how far back to look for the latest point (default '-1h').
        """
        _check_ident("bucket", bucket)
        _check_ident("measurement", measurement)
        _check_range(since)

        lines = [
            f"from(bucket: {_flux_string(bucket)})",
            f"  |> range(start: {since})",
            f"  |> filter(fn: (r) => r._measurement == {_flux_string(measurement)})",
        ]
        if field is not None:
            _check_ident("field", field)
            lines.append(f"  |> filter(fn: (r) => r._field == {_flux_string(field)})")
        for key, value in (tags or {}).items():
            if not _TAGKEY_RE.match(key):
                raise HomelabMcpError(f"tag key {key!r} contains disallowed characters")
            lines.append(f'  |> filter(fn: (r) => r["{key}"] == {_flux_string(str(value))})')
        lines.append("  |> last()")
        lines.append(f"  |> limit(n: {_MAX_ROWS})")

        rows = await self._query("\n".join(lines))
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                {
                    "time": r.get("_time"),
                    "field": r.get("_field"),
                    "value": r.get("_value"),
                    "measurement": r.get("_measurement"),
                }
            )
        return out
