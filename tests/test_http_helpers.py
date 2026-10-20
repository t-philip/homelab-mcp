from __future__ import annotations

import httpx
import pytest
import respx

from homelab_mcp.adapters import _http
from homelab_mcp.adapters._http import build_client, get_json
from homelab_mcp.errors import UpstreamError

BASE = "https://svc.example.invalid"


@respx.mock
async def test_get_json_rejects_oversized_body_by_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 100)
    respx.get(f"{BASE}/big").mock(
        return_value=httpx.Response(200, headers={"content-length": "999999"}, json={"x": 1})
    )
    client = build_client(BASE)
    try:
        with pytest.raises(UpstreamError, match=r"cap|exceeded"):
            await get_json(client, "/big")
    finally:
        await client.aclose()


@respx.mock
async def test_get_json_rejects_oversized_body_while_streaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_http, "MAX_RESPONSE_BYTES", 50)

    def _no_length(request: httpx.Request) -> httpx.Response:
        # a response with no Content-Length forces the byte-counting path
        return httpx.Response(200, stream=_ChunkStream())

    respx.get(f"{BASE}/chunky").mock(side_effect=_no_length)
    client = build_client(BASE)
    try:
        with pytest.raises(UpstreamError, match=r"exceeded|bytes"):
            await get_json(client, "/chunky")
    finally:
        await client.aclose()


class _ChunkStream(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(50):
            yield b"x" * 200

    async def aclose(self) -> None:
        return None


@respx.mock
async def test_error_body_is_not_echoed_in_the_message() -> None:
    respx.get(f"{BASE}/boom").mock(
        return_value=httpx.Response(500, text="SECRET_TOKEN_abc123 leaked in body")
    )
    client = build_client(BASE)
    try:
        with pytest.raises(UpstreamError) as exc:
            await get_json(client, "/boom")
    finally:
        await client.aclose()
    assert "SECRET_TOKEN" not in str(exc.value)
    assert "HTTP 500" in str(exc.value)
