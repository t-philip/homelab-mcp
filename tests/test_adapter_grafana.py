from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters.grafana import GrafanaAdapter
from homelab_mcp.errors import HomelabMcpError

from .conftest import load_fixture_json, make_options

BASE = "https://grafana.example.invalid"


def _adapter() -> GrafanaAdapter:
    return GrafanaAdapter(
        make_options(
            raw={"base_url": BASE, "token_env": "GRAFANA_TOKEN", "verify_tls": True},
            secrets={"GRAFANA_TOKEN": "svc-token"},
        )
    )


@respx.mock
async def test_list_dashboards_and_auth_header() -> None:
    route = respx.get(f"{BASE}/api/search").mock(
        return_value=httpx.Response(200, json=load_fixture_json("grafana", "search.json"))
    )
    adapter = _adapter()
    try:
        rows = await adapter.grafana_list_dashboards()
    finally:
        await adapter.aclose()

    assert {r["uid"] for r in rows} == {"aaa111", "bbb222"}
    overview = next(r for r in rows if r["uid"] == "aaa111")
    assert overview["folder"] == "Test Folder"
    assert overview["tags"] == ["test", "overview"]
    assert next(r for r in rows if r["uid"] == "bbb222")["folder"] == "General"
    assert route.calls.last.request.headers["Authorization"] == "Bearer svc-token"


@respx.mock
async def test_dashboard_panels() -> None:
    respx.get(f"{BASE}/api/dashboards/uid/aaa111").mock(
        return_value=httpx.Response(200, json=load_fixture_json("grafana", "dashboard_uid.json"))
    )
    adapter = _adapter()
    try:
        dash = await adapter.grafana_dashboard(uid="aaa111")
    finally:
        await adapter.aclose()

    assert dash["title"] == "Test Overview"
    assert dash["panel_count"] == 3
    assert dash["panels"] == ["CPU", "Memory"]  # the row panel has no title


@respx.mock
async def test_list_alerts_filter_by_state() -> None:
    respx.get(f"{BASE}/api/prometheus/grafana/api/v1/rules").mock(
        return_value=httpx.Response(200, json=load_fixture_json("grafana", "rules.json"))
    )
    adapter = _adapter()
    try:
        every = await adapter.grafana_list_alerts()
        firing = await adapter.grafana_list_alerts(state="firing")
    finally:
        await adapter.aclose()

    assert {r["name"] for r in every} == {"Test High CPU", "Test Disk Space"}
    assert [r["name"] for r in firing] == ["Test High CPU"]
    assert firing[0]["active_alerts"] == 1
    assert firing[0]["folder"] == "Test Folder"


async def test_list_alerts_rejects_bad_state() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="state must be one of"):
            await adapter.grafana_list_alerts(state="onfire")
    finally:
        await adapter.aclose()


@respx.mock
async def test_list_alerts_surfaces_grafana_error_payload() -> None:
    respx.get(f"{BASE}/api/prometheus/grafana/api/v1/rules").mock(
        return_value=httpx.Response(
            200, json={"status": "error", "error": "unified alerting is disabled"}
        )
    )
    adapter = _adapter()
    try:
        with pytest.raises(Exception, match="unified alerting is disabled"):
            await adapter.grafana_list_alerts()
    finally:
        await adapter.aclose()


@respx.mock
async def test_dashboard_uid_is_validated() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="disallowed characters"):
            await adapter.grafana_dashboard(uid="../admin/settings")
    finally:
        await adapter.aclose()


@respx.mock
async def test_probe() -> None:
    respx.get(f"{BASE}/api/health").mock(
        return_value=httpx.Response(200, json=load_fixture_json("grafana", "health.json"))
    )
    adapter = _adapter()
    try:
        result = await adapter.probe()
    finally:
        await adapter.aclose()
    assert result.ok
    assert "11.0.0" in result.detail
