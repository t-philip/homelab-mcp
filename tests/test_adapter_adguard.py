from __future__ import annotations

import httpx
import respx

from homelab_mcp.adapters.adguard import AdguardAdapter

from .conftest import load_fixture_json, make_options

BASE = "https://adguard.example.invalid"


def _adapter() -> AdguardAdapter:
    return AdguardAdapter(
        make_options(
            raw={
                "base_url": BASE,
                "username_env": "AGH_USER",
                "password_env": "AGH_PASS",
            },
            secrets={"AGH_USER": "admin", "AGH_PASS": "pw"},
        )
    )


@respx.mock
async def test_status_and_basic_auth() -> None:
    route = respx.get(f"{BASE}/control/status").mock(
        return_value=httpx.Response(200, json=load_fixture_json("adguard", "status.json"))
    )
    adapter = _adapter()
    try:
        status = await adapter.adguard_status()
    finally:
        await adapter.aclose()

    assert status["running"] is True
    assert status["protection_enabled"] is True
    assert route.calls.last.request.headers["Authorization"].startswith("Basic ")


@respx.mock
async def test_stats_and_top_lists_are_cached() -> None:
    route = respx.get(f"{BASE}/control/stats").mock(
        return_value=httpx.Response(200, json=load_fixture_json("adguard", "stats.json"))
    )
    adapter = _adapter()
    try:
        stats = await adapter.adguard_stats()
        clients = await adapter.adguard_top_clients(limit=2)
        domains = await adapter.adguard_top_domains(limit=1)
    finally:
        await adapter.aclose()

    assert route.call_count == 1  # one /control/stats for all three
    assert stats["num_dns_queries"] == 12000
    assert stats["avg_processing_time_ms"] == 12.5
    assert clients == [
        {"name": "192.0.2.10", "count": 4200},
        {"name": "192.0.2.11", "count": 3100},
    ]
    assert domains["queried"] == [{"name": "test-telemetry.example.invalid", "count": 1800}]
    assert domains["blocked"][0]["name"] == "test-ads.example.invalid"


@respx.mock
async def test_query_log_summary() -> None:
    route = respx.get(f"{BASE}/control/querylog").mock(
        return_value=httpx.Response(200, json=load_fixture_json("adguard", "querylog.json"))
    )
    adapter = _adapter()
    try:
        rows = await adapter.adguard_query_log(limit=5, search="example")
    finally:
        await adapter.aclose()

    assert rows[0]["name"] == "test-cdn.example.invalid"
    assert rows[1]["filtered"] is True
    assert rows[1]["status"] == "FilteredBlackList"
    assert route.calls.last.request.url.params["search"] == "example"
    assert route.calls.last.request.url.params["limit"] == "5"


@respx.mock
async def test_probe_reflects_running_flag() -> None:
    respx.get(f"{BASE}/control/status").mock(
        return_value=httpx.Response(200, json={"running": False, "version": "v0.1"})
    )
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert not result.ok
