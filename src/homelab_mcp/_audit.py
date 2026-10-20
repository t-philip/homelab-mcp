"""Static audit helpers for the read-only guarantee.

The tool-name rule (:mod:`homelab_mcp.naming`) stops a tool *called* something
write-shaped. This module adds the other half: it scans adapter source for HTTP
calls that are not GET, and fails unless each is on an explicit allowlist. A
handler that quietly did `client.post(...)` to a write endpoint would otherwise
pass every other check.
"""

from __future__ import annotations

import re
from pathlib import Path

ADAPTERS_DIR = Path(__file__).parent / "adapters"

#: (source filename, HTTP method, first string arg) tuples that are permitted.
#: The only legitimate non-GET is InfluxDB's query endpoint, which is a read.
ALLOWED_NON_GET: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("influxdb.py", "POST", "/api/v2/query"),
    }
)

# `\s*` spans newlines, and these run against the whole file (not line by line),
# so a call whose method/URL arguments are on continuation lines — the idiomatic
# way to write `client.stream("POST", "/x", ...)` — is still caught. An earlier
# line-by-line version missed InfluxDB's multi-line query call entirely, so its
# allowlist entry was never actually exercised.
_ATTR_CALL = re.compile(
    r"\.(?P<method>post|put|patch|delete)\(\s*(?P<url>[\"'][^\"']*[\"'])?",
    re.IGNORECASE,
)
_STREAM_CALL = re.compile(
    r"\.stream\(\s*[\"'](?P<method>[A-Za-z]+)[\"']\s*,\s*(?P<url>[\"'][^\"']*[\"'])?",
)


def _strip_quotes(literal: str | None) -> str:
    return literal[1:-1] if literal and len(literal) >= 2 else ""


def _lineno(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def find_non_get_http_calls() -> list[tuple[str, int, str, str]]:
    """Return ``(filename, lineno, method, url)`` for every disallowed call."""
    offenders: list[tuple[str, int, str, str]] = []
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in _ATTR_CALL.finditer(text):
            method = m.group("method").upper()
            url = _strip_quotes(m.group("url"))
            if (path.name, method, url) not in ALLOWED_NON_GET:
                offenders.append((path.name, _lineno(text, m.start()), method, url))
        for m in _STREAM_CALL.finditer(text):
            method = m.group("method").upper()
            url = _strip_quotes(m.group("url"))
            if method != "GET" and (path.name, method, url) not in ALLOWED_NON_GET:
                offenders.append((path.name, _lineno(text, m.start()), method, url))
    return offenders
