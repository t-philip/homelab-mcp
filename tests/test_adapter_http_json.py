from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters.http_json import HttpJsonAdapter, _resolve_path
from homelab_mcp.errors import ConfigError, HomelabMcpError

from .conftest import make_options

RAW = {
    "endpoints": {
        "health": {
            "url": "https://svc.example.invalid/api/health",
            "extract": "status",
            "headers": {"Accept": "application/json"},
        },
        "metrics": {
            "url": "https://svc.example.invalid/api/metrics",
            "token_env": "SVC_TOKEN",
        },
    }
}


def _adapter() -> HttpJsonAdapter:
    return HttpJsonAdapter(make_options(raw=RAW, secrets={"SVC_TOKEN": "abc123"}))


def test_resolve_path_walks_dicts_and_lists() -> None:
    doc = {"a": {"b": [{"c": 7}, {"c": 8}]}}
    assert _resolve_path(doc, "a.b.1.c") == 8


def test_missing_url_is_config_error() -> None:
    with pytest.raises(ConfigError, match="needs a 'url'"):
        HttpJsonAdapter(make_options(raw={"endpoints": {"bad": {"extract": "x"}}}))


async def test_list_endpoints_hides_secrets() -> None:
    adapter = _adapter()
    try:
        listed = await adapter.http_json_list_endpoints()
    finally:
        await adapter.aclose()
    assert listed == [
        {"name": "health", "url": "https://svc.example.invalid/api/health", "extract": "status"},
        {"name": "metrics", "url": "https://svc.example.invalid/api/metrics", "extract": None},
    ]


@respx.mock
async def test_get_applies_extract() -> None:
    respx.get("https://svc.example.invalid/api/health").mock(
        return_value=httpx.Response(200, json={"status": "green", "detail": "all good"})
    )
    adapter = _adapter()
    try:
        result = await adapter.http_json_get(name="health")
    finally:
        await adapter.aclose()
    assert result == "green"


@respx.mock
async def test_get_injects_bearer_token_and_returns_full_json() -> None:
    route = respx.get("https://svc.example.invalid/api/metrics").mock(
        return_value=httpx.Response(200, json={"a": 1, "b": 2})
    )
    adapter = _adapter()
    try:
        result = await adapter.http_json_get(name="metrics")
    finally:
        await adapter.aclose()
    assert result == {"a": 1, "b": 2}
    assert route.calls.last.request.headers["Authorization"] == "Bearer abc123"


async def test_unknown_endpoint_rejected() -> None:
    adapter = _adapter()
    try:
        with pytest.raises(HomelabMcpError, match="unknown endpoint"):
            await adapter.http_json_get(name="nope")
    finally:
        await adapter.aclose()


async def test_missing_token_env_is_reported() -> None:
    adapter = HttpJsonAdapter(make_options(raw=RAW, secrets={}))  # SVC_TOKEN absent
    try:
        with pytest.raises(HomelabMcpError, match=r"\$SVC_TOKEN is not set"):
            await adapter.http_json_get(name="metrics")
    finally:
        await adapter.aclose()


def test_invalid_endpoint_url_is_config_error() -> None:
    for bad in ("not-a-url", "ftp://x/y", "/relative/only", "http://"):
        with pytest.raises(ConfigError, match="url"):
            HttpJsonAdapter.validate(make_options(raw={"endpoints": {"e": {"url": bad}}}))
