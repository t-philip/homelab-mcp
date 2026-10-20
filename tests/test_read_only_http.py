"""The other half of the read-only guarantee: no adapter makes a non-GET HTTP
call outside the explicit allowlist."""

from __future__ import annotations

from homelab_mcp._audit import ALLOWED_NON_GET, find_non_get_http_calls


def test_no_disallowed_non_get_http_calls() -> None:
    assert find_non_get_http_calls() == []


def test_allowlist_is_minimal() -> None:
    # If this changes, a reviewer must consciously bless the new non-GET call.
    assert frozenset({("influxdb.py", "POST", "/api/v2/query")}) == ALLOWED_NON_GET


def test_audit_would_catch_a_planted_write(tmp_path: object) -> None:
    from homelab_mcp import _audit

    line = 'x = await client.post("/api/v2/write", content=body)'
    matches = list(_audit._ATTR_CALL.finditer(line))
    assert matches and matches[0].group("method") == "post"


def test_audit_sees_multiline_stream_calls() -> None:
    """InfluxDB's POST query is written across lines; the scan must still see it,
    so ALLOWED_NON_GET is genuinely exercised rather than dead."""
    import unittest.mock

    from homelab_mcp import _audit

    with unittest.mock.patch.object(_audit, "ALLOWED_NON_GET", frozenset()):
        offenders = _audit.find_non_get_http_calls()
    assert ("influxdb.py", "POST", "/api/v2/query") in {(n, m, u) for n, _lineno, m, u in offenders}
