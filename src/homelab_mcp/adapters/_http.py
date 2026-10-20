"""Shared HTTP plumbing for adapters that talk to a REST API.

Every response is read with a byte ceiling (``MAX_RESPONSE_BYTES``) so a large
or hostile upstream cannot exhaust memory on a small container. Upstream error
*bodies* are logged, never returned in the error message that reaches the MCP
client — a backend that echoes a token or PII in a 4xx/5xx body must not leak it
onward.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx

from ..errors import UpstreamError

logger = logging.getLogger("homelab_mcp.http")

DEFAULT_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def build_client(
    base_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    verify_tls: bool = True,
    timeout: float = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    if not verify_tls:
        logger.warning(
            "TLS verification is disabled for %s — only acceptable on a trusted LAN",
            base_url,
        )
    return httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        headers=dict(headers or {}),
        verify=verify_tls,
        timeout=timeout,
        follow_redirects=False,
    )


async def read_capped(response: httpx.Response) -> bytes:
    """Read a streamed response body, aborting past :data:`MAX_RESPONSE_BYTES`."""
    declared = response.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise UpstreamError(
            f"{response.request.url.path}: response is {declared} bytes (cap {MAX_RESPONSE_BYTES})"
        )
    chunks: list[bytes] = []
    total = 0
    async for chunk in response.aiter_bytes():
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise UpstreamError(
                f"{response.request.url.path}: response exceeded {MAX_RESPONSE_BYTES} bytes"
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def request_json(client: httpx.AsyncClient, method: str, path: str, **kwargs: Any) -> Any:
    """Perform a size-capped request and return parsed JSON.

    Failures map to :class:`UpstreamError`; the error message carries the status
    only, and the body (if any) is logged at WARNING.
    """
    try:
        async with client.stream(method, path, **kwargs) as response:
            if response.status_code >= 400:
                body = (await read_capped(response)).decode("utf-8", "replace")
                logger.warning(
                    "%s %s -> HTTP %s: %s",
                    method,
                    path,
                    response.status_code,
                    body[:500],
                )
                raise UpstreamError(f"{path} returned HTTP {response.status_code}")
            raw = await read_capped(response)
    except httpx.HTTPError as exc:
        logger.warning("%s %s failed: %s", method, path, exc)
        raise UpstreamError(f"request to {path} failed: {exc}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise UpstreamError(f"{path} did not return JSON") from exc


async def get_json(client: httpx.AsyncClient, path: str, **kwargs: Any) -> Any:
    """GET *path* and return parsed JSON (size-capped)."""
    return await request_json(client, "GET", path, **kwargs)
