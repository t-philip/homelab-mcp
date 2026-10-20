from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from homelab_mcp.adapters.pinglog import PinglogAdapter

from .conftest import make_options


def _write_csv(path: Path) -> None:
    """A fixture built from `now` — relative, so it carries no calendar fact."""
    now = datetime.now(tz=UTC)
    lines = ["timestamp,target,success,rtt_ms"]
    for minutes_ago, target, ok in [
        (2, "gateway", "true"),
        (5, "gateway", "true"),
        (9, "gateway", "false"),
        (2, "nas", "true"),
        (90, "gateway", "true"),  # outside a 60-min window
    ]:
        ts = (now - timedelta(minutes=minutes_ago)).isoformat()
        rtt = "3.1" if ok == "true" else ""
        lines.append(f"{ts},{target},{ok},{rtt}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _adapter(path: Path, **extra: object) -> PinglogAdapter:
    return PinglogAdapter(make_options(raw={"path": str(path), **extra}))


async def test_summary_windows_and_uptime(tmp_path: Path) -> None:
    log = tmp_path / "ping.csv"
    _write_csv(log)
    adapter = _adapter(log)
    rows = await adapter.pinglog_summary(window_minutes=60)

    by_target = {r["target"]: r for r in rows}
    assert set(by_target) == {"gateway", "nas"}
    gw = by_target["gateway"]
    assert gw["samples"] == 3  # the 90-min-old row is excluded
    assert gw["up"] == 2
    assert gw["down"] == 1
    assert gw["uptime_pct"] == 66.7
    assert gw["last_status"] == "up"  # newest in-window row is 2 min old, success
    assert by_target["nas"]["uptime_pct"] == 100.0


async def test_target_history_limit(tmp_path: Path) -> None:
    log = tmp_path / "ping.csv"
    _write_csv(log)
    adapter = _adapter(log)
    hist = await adapter.pinglog_target_history(target="gateway", limit=2)
    assert len(hist) == 2
    assert {h["status"] for h in hist} <= {"up", "down"}


async def test_jsonl_format(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    log = tmp_path / "ping.jsonl"
    log.write_text(
        "\n".join(
            f'{{"timestamp": "{(now - timedelta(minutes=m)).isoformat()}", '
            f'"target": "web", "success": {v}}}'
            for m, v in [(1, "true"), (3, "false")]
        )
        + "\n",
        encoding="utf-8",
    )
    adapter = _adapter(log, format="jsonl")
    rows = await adapter.pinglog_summary(window_minutes=30)
    assert rows[0]["target"] == "web"
    assert rows[0]["up"] == 1
    assert rows[0]["down"] == 1


async def test_probe_missing_file(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path / "nope.csv")
    result = await adapter.probe()
    assert not result.ok
    assert "does not exist" in result.detail


async def test_probe_ok(tmp_path: Path) -> None:
    log = tmp_path / "ping.csv"
    _write_csv(log)
    result = await _adapter(log).probe()
    assert result.ok
    assert "samples" in result.detail


async def test_trimmed_tail_reads_header_from_the_front(tmp_path: Path) -> None:
    """A large file whose tail has no header must not be read whole."""
    now = datetime.now(tz=UTC)
    log = tmp_path / "ping.csv"
    rows = ["timestamp,target,success,rtt_ms"]
    for i in range(4000):
        ts = (now - timedelta(minutes=i % 30)).isoformat()
        rows.append(f"{ts},gw,{'true' if i % 3 else 'false'},1.0")
    log.write_text("\n".join(rows) + "\n", encoding="utf-8")

    # cap the tail well below the file size so the tail has no header line
    adapter = _adapter(log, max_bytes=2000)
    summary = await adapter.pinglog_summary(window_minutes=60)
    assert summary and summary[0]["target"] == "gw"
    assert summary[0]["samples"] < 4000  # only the tail was parsed


async def test_naive_timestamps_honour_configured_timezone(tmp_path: Path) -> None:
    """A naive timestamp is localised to [adapters.pinglog].timezone."""
    log = tmp_path / "ping.csv"
    naive = "1999-06-07T12:00:00"
    log.write_text(f"timestamp,target,success\n{naive},gw,true\n", encoding="utf-8")

    utc_hist = await _adapter(log).pinglog_target_history(target="gw", limit=1)
    local_hist = await _adapter(log, timezone="America/New_York").pinglog_target_history(
        target="gw", limit=1
    )
    utc_dt = datetime.fromisoformat(utc_hist[0]["time"])
    local_dt = datetime.fromisoformat(local_hist[0]["time"])
    # same wall clock, different instant: New York was UTC-4 on that date
    assert utc_dt - local_dt == timedelta(hours=-4)


async def test_all_unknown_status_is_flagged_by_probe(tmp_path: Path) -> None:
    """A misconfigured status column should not read as healthy."""
    now = datetime.now(tz=UTC)
    log = tmp_path / "ping.csv"
    log.write_text(
        "timestamp,target,ok\n"
        + "\n".join(f"{(now - timedelta(minutes=m)).isoformat()},gw,yes" for m in range(5))
        + "\n",
        encoding="utf-8",
    )
    # 'ok'/'yes' don't match the default success/rtt columns -> all unknown
    result = await _adapter(log).probe()
    assert not result.ok
    assert "unknown status" in result.detail


async def test_bad_timezone_is_config_error() -> None:
    import pytest

    from homelab_mcp.errors import ConfigError

    with pytest.raises(ConfigError, match="IANA zone"):
        PinglogAdapter.validate(make_options(raw={"path": "x", "timezone": "Mars/Olympus"}))


def test_millisecond_epoch_and_non_finite_do_not_crash(tmp_path: Path) -> None:
    import json as _json

    from homelab_mcp.adapters.pinglog import _parse_time

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    parsed = _parse_time(now_ms, UTC)
    assert parsed is not None and abs((parsed - datetime.now(tz=UTC)).total_seconds()) < 5
    assert _parse_time(float("inf"), UTC) is None
    assert _parse_time(float("nan"), UTC) is None
    assert _parse_time(10**18, UTC) is None  # absurd even as ms

    log = tmp_path / "p.jsonl"
    log.write_text(
        _json.dumps({"timestamp": now_ms, "target": "gw", "success": "true"})
        + "\n"
        + '{"timestamp": Infinity, "target": "gw", "success": "true"}\n',
        encoding="utf-8",
    )
    rows = _adapter(log, format="jsonl").tools()
    assert rows  # constructed fine; the real assertion is "no exception above"


async def test_target_history_is_sorted_newest_last(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    log = tmp_path / "rev.csv"
    # rows written newest-first (some monitors prepend)
    lines = ["timestamp,target,success"]
    for m in (1, 3, 7, 15):
        lines.append(f"{(now - timedelta(minutes=m)).isoformat()},gw,true")
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    hist = await _adapter(log).pinglog_target_history(target="gw", limit=2)
    assert [h["time"] for h in hist] == sorted(h["time"] for h in hist)
    # the two most recent are 1 and 3 minutes ago
    assert hist[-1]["time"] > hist[0]["time"]


def test_bad_max_bytes_is_config_error() -> None:
    import pytest

    from homelab_mcp.errors import ConfigError

    with pytest.raises(ConfigError, match="max_bytes"):
        PinglogAdapter.validate(make_options(raw={"path": "x", "max_bytes": "5MB"}))
