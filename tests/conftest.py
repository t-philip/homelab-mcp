"""Shared test helpers.

All fixture data under ``tests/fixtures/`` is fictitious: hosts are ``node-a`` /
``node-b``, addresses are in the documentation ranges (192.0.2.0/24,
198.51.100.0/24), free text is ``Test Data …``, and every date is before 2002.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from homelab_mcp.adapters.base import AdapterOptions
from homelab_mcp.cache import TTLCache

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture_text(*parts: str) -> str:
    return (FIXTURES.joinpath(*parts)).read_text(encoding="utf-8")


def load_fixture_json(*parts: str) -> Any:
    return json.loads(load_fixture_text(*parts))


@pytest.fixture
def cache() -> TTLCache:
    return TTLCache(ttl_seconds=15.0)


@pytest.fixture
def no_cache() -> TTLCache:
    return TTLCache(ttl_seconds=0)


def make_options(
    raw: dict[str, Any] | None = None,
    *,
    secrets: dict[str, str] | None = None,
    cache: TTLCache | None = None,
) -> AdapterOptions:
    return AdapterOptions(
        raw=raw or {},
        secrets=secrets or {},
        cache=cache or TTLCache(ttl_seconds=15.0),
    )
