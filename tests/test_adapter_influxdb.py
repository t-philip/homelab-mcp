from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters.influxdb import InfluxdbAdapter, _parse_annotated_csv
from homelab_mcp.errors import HomelabMcpError

from .conftest import load_fixture_json, load_fixture_text, make_options

BASE = "https://influx.example.invalid"


def _adapter() -> InfluxdbAdapter:
    return InfluxdbAdapter(
        make_options(
            raw={"base_url": BASE, "token_env": "INFLUX_TOKEN", "org": "test-org"},
            secrets={"INFLUX_TOKEN": "tok"},
        )
    )


def test_parse_annotated_csv_splits_tables_on_blank_line() -> None:
    rows = _parse_annotated_csv(load_fixture_text("influxdb", "last.csv"))
    assert len(rows) == 1
    assert rows[0]["_value"] == "0.42"
    assert rows[0]["_field"] == "usage"
    assert rows[0]["_measurement"] == "cpu"


@respx.mock
async def test_list_buckets() -> None:
    respx.get(f"{BASE}/api/v2/buckets").mock(
        return_value=httpx.Response(200, json=load_fixture_json("influxdb", "buckets.json"))
    )
    adapter = _adapter()
    try:
        buckets = await adapter.influxdb_list_buckets()
    finally:
        await adapter.aclose()
    assert {b["name"] for b in buckets} == {"test-metrics", "test-events"}
    assert next(b for b in buckets if b["name"] == "test-metrics")["retention_seconds"] == 2592000


@respx.mock
async def test_measurements_query() -> None:
    route = respx.post(f"{BASE}/api/v2/query").mock(
        return_value=httpx.Response(200, text=load_fixture_text("influxdb", "measurements.csv"))
    )
    adapter = _adapter()
    try:
        names = await adapter.influxdb_measurements(bucket="test-metrics", since="-6h")
    finally:
        await adapter.aclose()

    assert names == ["cpu", "mem", "net"]
    body = route.calls.last.request.content.decode()
    assert 'schema.measurements(bucket: "test-metrics"' in body
    assert "start: -6h" in body
    assert route.calls.last.request.url.params["org"] == "test-org"


@respx.mock
async def test_latest_value_builds_safe_flux() -> None:
    route = respx.post(f"{BASE}/api/v2/query").mock(
        return_value=httpx.Response(200, text=load_fixture_text("influxdb", "last.csv"))
    )
    adapter = _adapter()
    try:
        rows = await adapter.influxdb_latest_value(
            bucket="test-metrics",
            measurement="cpu",
            field="usage",
            tags={"host": "node-a"},
            since="-30m",
        )
    finally:
        await adapter.aclose()

    assert rows == [
        {"time": "1999-06-07T07:59:00Z", "field": "usage", "value": "0.42", "measurement": "cpu"}
    ]
    flux = route.calls.last.request.content.decode()
    assert 'from(bucket: "test-metrics")' in flux
    assert "range(start: -30m)" in flux
    assert 'r._measurement == "cpu"' in flux
    assert 'r._field == "usage"' in flux
    assert 'r["host"] == "node-a"' in flux
    assert "|> last()" in flux


async def test_rejects_injection_attempts() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="disallowed characters"):
            await adapter.influxdb_measurements(bucket='m") |> drop(', since="-1h")
        with pytest.raises(HomelabMcpError, match="negative Flux duration"):
            await adapter.influxdb_measurements(bucket="ok", since="1h ago")
        with pytest.raises(HomelabMcpError, match="looks back too far"):
            await adapter.influxdb_measurements(bucket="ok", since="-99999d")
        with pytest.raises(HomelabMcpError, match="not allowed in a filter"):
            await adapter.influxdb_latest_value(
                bucket="b", measurement="m", tags={"host": 'a" or true or "'}
            )
        # Flux string interpolation `${...}` must not survive into a predicate
        for bad in ("${1+1}", "a${r._value}b", "x`y`z", "line\nbreak"):
            with pytest.raises(HomelabMcpError, match="not allowed in a filter"):
                await adapter.influxdb_latest_value(bucket="b", measurement="m", tags={"host": bad})
        with pytest.raises(HomelabMcpError, match="tag key"):
            await adapter.influxdb_latest_value(bucket="b", measurement="m", tags={"ho st": "x"})
    finally:
        await adapter.aclose()


@respx.mock
async def test_probe() -> None:
    respx.get(f"{BASE}/health").mock(
        return_value=httpx.Response(200, json=load_fixture_json("influxdb", "health.json"))
    )
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert result.ok
    assert "2.7.0" in result.detail
